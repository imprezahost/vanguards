#!/usr/bin/env python

import random
import os
import time
import pickle
import sys
import string

from ipaddress import ip_network as net

import stem

from .NodeSelection import BwWeightedGenerator, NodeRestrictionList
from .NodeSelection import FlagsRestriction
from .logger import plog

from . import control
from . import rendguard

# Unicode, damnit
try:
  _UNICODE_DAMNIT = bool(type(unicode))
except NameError:
  unicode = str

################### Vanguard options ##################
#
NUM_LAYER1_GUARDS = 2 # 0 is Tor default
NUM_LAYER2_GUARDS = 3
NUM_LAYER3_GUARDS = 8

# In days:
LAYER1_LIFETIME_DAYS = 0 # Use tor default

# In hours
MIN_LAYER2_LIFETIME_HOURS = 24*1
MAX_LAYER2_LIFETIME_HOURS = 24*45

# In hours
MIN_LAYER3_LIFETIME_HOURS = 1
MAX_LAYER3_LIFETIME_HOURS = 48

_SEC_PER_HOUR = (60*60)

class GuardNode:
  def __init__(self, idhex, chosen_at, expires_at):
    self.idhex = idhex
    self.chosen_at = chosen_at
    self.expires_at = expires_at

class ExcludeNodes:
  def __init__(self, controller):
    self.networks = []
    self.idhexes = set()
    self.nicks = set()
    self.countries = set()
    self.controller = controller
    self.exclude_unknowns = controller.get_conf("GeoIPExcludeUnknown")
    self._parse_line(controller.get_conf("ExcludeNodes"))

  def _parse_line(self, conf_line):
    # We assume Tor has validated the line already. So this parsing
    # is very dumb+simple (except for semantics of valid data).
    # See routerset_parse() in tor for parser semantic ordering.
    if self.exclude_unknowns == "1":
      self.countries.add("??")
      self.countries.add("a1")

    if conf_line == None:
      return

    parts = conf_line.split(",")
    for p in parts:
      if p[0] == "$":
        p = p[1:]
      if "~" in p:
        p = p.split("~")[0]
      if "=" in p:
        p = p.split("=")[0]

      if len(p) == 40 and all(c in string.hexdigits for c in p):
        self.idhexes.add(p)
      elif p[0] == "{" and p[-1] == "}":
        self.countries.add(p[1:-1].lower())
      elif ":" in p or "." in p:
        self.networks.append(net(unicode(p), strict=False))
      else:
        self.nicks.add(p)
    plog("INFO", "Honoring ExcludeNodes line: "+conf_line)
    if len(self.networks):
      plog("INFO", "Excluding networks "+str(self.networks))
    if len(self.idhexes):
      plog("INFO", "Excluding idhexes "+str(self.idhexes))
    if len(self.nicks):
      plog("INFO", "Excluding nicks "+str(self.nicks))
    if len(self.countries):
      if self.exclude_unknowns == "auto":
        self.countries.add("??")
        self.countries.add("a1")

      if self.controller.get_info("ip-to-country/ipv4-available", "0") == "0":
        plog("WARN", "ExcludeNodes contains countries, but Tor has no GeoIP file! "+
             "Tor is not excluding countries!")
      else:
        plog("INFO", "Excluding countries "+str(self.countries))

  def router_is_excluded(self, r):
    if r.fingerprint in self.idhexes:
      return True
    if r.nickname in self.nicks:
      return True
    if "or_addresses" in r.__dict__: # Stem 1.7.0 only
      addresses = r.or_addresses
    else:
      addresses = [(r.address, 9001, False)]
    for addr in addresses:
      is_ipv6 = addr[2]
      if len(self.countries):
        country = None
        if is_ipv6 and \
          self.controller.get_info("ip-to-country/ipv6-available", "0") == "1":
          country = self.controller.get_info("ip-to-country/"+addr[0])
        if not is_ipv6 and \
          self.controller.get_info("ip-to-country/ipv4-available", "0") == "1":
          country = self.controller.get_info("ip-to-country/"+addr[0])

        if country != None and country.lower() in self.countries:
          return True

      for network in self.networks:
        if is_ipv6:
          if network.version == 6 and net(addr[0]+"/128").overlaps(network):
            return True
        else:
          if network.version == 4 and net(addr[0]+"/32").overlaps(network):
            return True
    return False

class VanguardState:
  def __init__(self, state_file):
    self.layer2 = []
    self.layer3 = []
    self.state_file = state_file
    self.rendguard = rendguard.RendGuard()
    self.pickle_revision = 1
    self.enable_vanguards = True # Set from main, irrelevant to pickle

  def set_state_file(self, state_file):
    self.state_file = state_file

  def sort_and_index_routers(self, routers, descs):
    sorted_r = list(routers)
    dict_r = {}
    dict_d = {}

    for r in sorted_r:
      dict_r[r.fingerprint] = r

    for d in descs:
      dict_d[d.fingerprint] = d

    for r in sorted_r:
      if r.measured == None:
        # FIXME: Hrmm...
        r.measured = r.bandwidth
        r.old_measured = r.measured

      if r.fingerprint in dict_d and dict_d[r.fingerprint].observed_bandwidth:
        r.obs_bw = max(dict_d[r.fingerprint].observed_bandwidth,1)
        r.old_measured = r.measured
        r.measured = float(1000*r.measured)/r.obs_bw
      else:
        #print("No r: "+r.fingerprint)
        r.measured = 0
        r.old_measured = r.measured
        r.obs_bw = 0

    print("\n")
    if "A69221A7EC7498D2F88A0FB795261013FA36CAAE" in dict_r:
      s = dict_r["A69221A7EC7498D2F88A0FB795261013FA36CAAE"]
      print("# dgoulet guard: "+str(1000*s.old_measured)+"/"+str(s.obs_bw)+" = "+str(s.measured))

    if "303509AB910EF207B7438C27435C4A2FD579F1B1" in dict_r:
      s = dict_r["303509AB910EF207B7438C27435C4A2FD579F1B1"]
      print("# ahf guard1: "+str(1000*s.old_measured)+"/"+str(s.obs_bw)+" = "+str(s.measured))

    if "56927E61B51E6F363FB55498150A6DDFCF7077F2" in dict_r:
      s = dict_r["56927E61B51E6F363FB55498150A6DDFCF7077F2"]
      print("# ahf guard2: "+str(1000*s.old_measured)+"/"+str(s.obs_bw)+" = "+str(s.measured))

    sorted_r.sort(key = lambda x: x.measured, reverse = True)

    return (sorted_r, dict_r)

  def consensus_update(self, routers, weights, exclude, descs):
    (sorted_r, dict_r) = self.sort_and_index_routers(routers, descs)
    ng = BwWeightedGenerator(sorted_r,
                       NodeRestrictionList(
                             [FlagsRestriction(["Fast", "Stable", "Valid"],
                                               ["Authority"])]),
                             weights, BwWeightedGenerator.POSITION_MIDDLE)
    gen = ng.generate()
    if self.enable_vanguards:
      # Remove any nodes that are now down in the consensus
      self.remove_down_from_layer(self.layer2, dict_r)
      self.remove_down_from_layer(self.layer3, dict_r)

      # Remove any nodes whose rotation times are past due.
      # FIXME: We should check this more often... But we also
      # need to replenish our layers if they get too low/empty.
      # This can be slow (consensus parse required)... :/
      self.remove_expired_from_layer(self.layer2)
      self.remove_expired_from_layer(self.layer3)

      # Remove any nodes in case ExcludeNodes changed.
      self.remove_excluded_from_layer(self.layer2, dict_r, exclude)
      self.remove_excluded_from_layer(self.layer3, dict_r, exclude)

      # Replenish our guard lists with new nodes
      self.replenish_layers(gen, exclude)

    ng = BwWeightedGenerator(sorted_r,
                       NodeRestrictionList(
                             [FlagsRestriction(["Fast", "Valid", "Guard"],
                                               ["Authority", "Exit"])]),
                             weights, BwWeightedGenerator.POSITION_MIDDLE)

    print("\n# Client Side torrc entries:")

    for r in [0,2,4]:
      s = ng.rstr_routers[r]
      print("Bridge "+s.address+":"+str(s.or_port)+" "+s.fingerprint+" # ratio="+str(s.measured))

    print("\nHSLayer2Nodes ", end='')
    for r in [1,2,3,4,5,6]:
      s = ng.rstr_routers[r*2+6]
      print(s.fingerprint+",", end='')

    print("\nHSLayer3Nodes ", end='')
    for r in [1,2,3,4,5,6,7,8,9]:
      s = ng.rstr_routers[(r+6)*2]
      print(s.fingerprint+',', end='')

    print("\n\n# Service Side torrc entries:", end='')
    for r in [1,3,5]:
      s = ng.rstr_routers[r]
      print("\nBridge "+s.address+":"+str(s.or_port)+" "+s.fingerprint+" # ratio="+str(s.measured), end='')

    print("\nHSLayer2Nodes ", end='')
    for r in [1,2,3,4,5,6]:
      s = ng.rstr_routers[r*2+7]
      print(s.fingerprint+",", end='')

    print("\nHSLayer3Nodes ", end='')
    for r in [1,2,3,4,5,6,7,8,9]:
      s = ng.rstr_routers[(r+6)*2+1]
      print(s.fingerprint+',', end='')

    print("\nEnd")

    # Repair Exit-flagged node weights, since they can be chosen
    # sometimes by other clients as RPs (when cannibalized)
    ng.repair_exits()
    # Transfer and scale RP use counts to this consensus
    self.rendguard.xfer_use_counts(ng)

  def new_consensus_event(self, controller, event):
    routers = controller.get_network_statuses()
    descs = controller.get_server_descriptors()

    exclude_nodes = ExcludeNodes(controller)

    data_dir = controller.get_conf("DataDirectory")
    if data_dir == None:
      plog("ERROR",
           "You must set a DataDirectory location option in your torrc.")
      sys.exit(1)

    consensus_file = os.path.join(controller.get_conf("DataDirectory"),
                             "cached-microdesc-consensus")

    try:
      weights = control.get_consensus_weights(consensus_file)
    except IOError as e:
      raise stem.DescriptorUnavailable("Cannot read "+consensus_file+": "+str(e))

    self.consensus_update(routers, weights, exclude_nodes, descs)

    if self.enable_vanguards:
      self.configure_tor(controller)

    try:
      self.write_to_file(open(self.state_file, "wb"))
    except IOError as e:
      plog("ERROR", "Cannot write state to "+self.state_file+": "+str(e))
      sys.exit(1)

  def signal_event(self, controller, event):
    if event.signal == "RELOAD":
      plog("NOTICE", "Tor got SIGHUP. Reapplying vanguards.")
      self.configure_tor(controller)

  def configure_tor(self, controller):
    if NUM_LAYER1_GUARDS:
      controller.set_conf("NumEntryGuards", str(NUM_LAYER1_GUARDS))
      try:
        controller.set_conf("NumPrimaryGuards", str(NUM_LAYER1_GUARDS))
      except stem.InvalidArguments: # pre-0.3.4 tor
        pass

    if LAYER1_LIFETIME_DAYS > 0:
      controller.set_conf("GuardLifetime", str(LAYER1_LIFETIME_DAYS)+" days")

    try:
      controller.set_conf("HSLayer2Nodes", self.layer2_guardset())

      if NUM_LAYER3_GUARDS:
        controller.set_conf("HSLayer3Nodes", self.layer3_guardset())
    except stem.InvalidArguments:
      plog("ERROR",
           "Vanguards requires Tor 0.3.3.x (and ideally 0.3.4.x or newer).")
      sys.exit(1)

  def write_to_file(self, outfile):
    return pickle.dump(self, outfile)

  @staticmethod
  def read_from_file(infile):
    ret = pickle.load(open(infile, "rb"))
    ret.set_state_file(infile)
    return ret

  def layer2_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer2))

  def layer3_guardset(self):
    return ",".join(map(lambda g: g.idhex, self.layer3))

  # Adds a new layer2 guard
  def add_new_layer2(self, generator, excluded):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer2) or \
      excluded.router_is_excluded(guard):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(MIN_LAYER2_LIFETIME_HOURS*_SEC_PER_HOUR,
                                       MAX_LAYER2_LIFETIME_HOURS*_SEC_PER_HOUR),
                        random.uniform(MIN_LAYER2_LIFETIME_HOURS*_SEC_PER_HOUR,
                                       MAX_LAYER2_LIFETIME_HOURS*_SEC_PER_HOUR))
    self.layer2.append(GuardNode(guard.fingerprint, now, expires))
    plog("INFO", "New layer2 guard: "+guard.fingerprint)

  def add_new_layer3(self, generator, excluded):
    guard = next(generator)
    while guard.fingerprint in map(lambda g: g.idhex, self.layer3) or \
      excluded.router_is_excluded(guard):
      guard = next(generator)

    now = time.time()
    expires = now + max(random.uniform(MIN_LAYER3_LIFETIME_HOURS*_SEC_PER_HOUR,
                                       MAX_LAYER3_LIFETIME_HOURS*_SEC_PER_HOUR),
                        random.uniform(MIN_LAYER3_LIFETIME_HOURS*_SEC_PER_HOUR,
                                       MAX_LAYER3_LIFETIME_HOURS*_SEC_PER_HOUR))
    self.layer3.append(GuardNode(guard.fingerprint, now, expires))
    plog("INFO", "New layer3 guard: "+guard.fingerprint)

  def remove_excluded_from_layer(self, layer, dict_r, excluded):
    for g in list(layer):
      if excluded.router_is_excluded(dict_r[g.idhex]):
        layer.remove(g)
        plog("INFO", "Removing newly-excluded guard "+g.idhex)

  def remove_expired_from_layer(self, layer):
    now = time.time()
    for g in list(layer):
      if g.expires_at < now:
        layer.remove(g)
        plog("INFO", "Removing expired guard "+g.idhex)

  def remove_down_from_layer(self, layer, dict_r):
    for g in list(layer):
      if not g.idhex in dict_r:
        layer.remove(g)
        plog("INFO", "Removing down guard "+g.idhex)

  def replenish_layers(self, generator, excluded):
    # Trim layers in case params changed
    self.layer2 = self.layer2[:NUM_LAYER2_GUARDS]
    self.layer3 = self.layer3[:NUM_LAYER3_GUARDS]

    while len(self.layer2) < NUM_LAYER2_GUARDS:
      self.add_new_layer2(generator, excluded)

    while len(self.layer3) < NUM_LAYER3_GUARDS:
      self.add_new_layer3(generator, excluded)
