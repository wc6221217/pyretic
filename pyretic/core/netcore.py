
################################################################################
# The Pyretic Project                                                          #
# frenetic-lang.org/pyretic                                                    #
# author: Joshua Reich (jreich@cs.princeton.edu)                               #
# author: Christopher Monsanto (chris@monsan.to)                               #
################################################################################
# Licensed to the Pyretic Project by one or more contributors. See the         #
# NOTICES file distributed with this work for additional information           #
# regarding copyright and ownership. The Pyretic Project licenses this         #
# file to you under the following license.                                     #
#                                                                              #
# Redistribution and use in source and binary forms, with or without           #
# modification, are permitted provided the following conditions are met:       #
# - Redistributions of source code must retain the above copyright             #
#   notice, this list of conditions and the following disclaimer.              #
# - Redistributions in binary form must reproduce the above copyright          #
#   notice, this list of conditions and the following disclaimer in            #
#   the documentation or other materials provided with the distribution.       #
# - The names of the copyright holds and contributors may not be used to       #
#   endorse or promote products derived from this work without specific        #
#   prior written permission.                                                  #
#                                                                              #
# Unless required by applicable law or agreed to in writing, software          #
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT    #
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the     #
# LICENSE file distributed with this work for specific language governing      #
# permissions and limitations under the License.                               #
################################################################################

# This module is designed for import *.
import functools
import itertools
import struct
from collections import Counter
import time
from bitarray import bitarray

from pyretic.core import util
from pyretic.core.network import *
from pyretic.core.util import frozendict, singleton


################################################################################
# Matching
################################################################################

class ExactMatch(object):
    """Pattern type for exact match"""

    def __init__(self, pattern):
        self.pattern = pattern

    def match(self, other):
        return self.pattern == other

    def __hash__(self):
        return hash(self.pattern)

    def __eq__(self, other):
        """Match by checking for equality"""
        return self.pattern == other.pattern 
        
    def __repr__(self):
        return repr(self.pattern)

class PrefixMatch(object):
    """Pattern type for IP prefix match"""

    def __init__(self, pattern):
        self.masklen = 32
        if isinstance(pattern, IP):     # IP OBJECT
            self.pattern = pattern
        else:                           # STRING ENCODING
            parts = pattern.split("/")
            self.pattern = IP(parts[0])
            if len(parts) == 2:
                self.masklen = int(parts[1])
        self.prefix = self.pattern.to_bits()[:self.masklen]

    def match(self, other):
        """Match by checking prefix equality"""
        return self.prefix == other.to_bits()[:self.masklen]

    def __hash__(self):
        return hash(self.pattern)

    def __eq__(self, other):
        return self.pattern == other.pattern 
        
    def __repr__(self):
        if self.masklen == 32:
            return repr(self.pattern)
        else:
            return "%s/%d" % (repr(self.pattern),self.masklen)

################################################################################
# Determine how each field will be matched
################################################################################
        
_field_to_patterntype = {}

def register_field(field, patterntype):
    _field_to_patterntype[field] = patterntype

def field_patterntype(field):
    return _field_to_patterntype.get(field, ExactMatch)

register_field("srcip", PrefixMatch)
register_field("dstip", PrefixMatch)

################################################################################
# Netcore Policy Language
################################################################################

class NetworkEvaluated(object):
    def __init__(self):
        self._network = None

    @property
    def network(self):
        return self._network
        
    def set_network(self, network):
        if network == self._network:
            return 
        self._network = network

    def eval(self, packet):
        raise NotImplementedError        

    def track_eval(self, packet):
        return (self.eval(packet),[self])

    def name(self):
        return self.__class__.__name__

    
################################################################################
# Predicates
################################################################################

class Predicate(NetworkEvaluated):
    """Top-level abstract class for predicates."""

    ### sub : Predicate -> Predicate
    def __sub__(self, other):
        return difference(self, other)
    
    ### and : Predicate -> Predicate 
    def __and__(self, other):
        return intersect([self, other])
    
    ### or : Predicate -> Predicate 
    def __or__(self, other):
        return union([self, other])

    ### getitem : Policy -> Policy 
    def __getitem__(self, policy):
        return restrict(policy, self)
        
    ### invert : unit -> Predicate
    def __invert__(self):
        return negate(self)

    ### eq : Predicate -> bool
    def __eq__(self, other):
        raise NotImplementedError

        
@singleton
class all_packets(Predicate):
    """The always-true predicate."""
    ### repr : unit -> String
    def __repr__(self):
        return "all packets"

    ### eval : Packet -> bool
    def eval(self, packet):
        return True
        
        
@singleton
class no_packets(Predicate):
    """The always-false predicate."""
    ### repr : unit -> String
    def __repr__(self):
        return "no packets"

    ### eval : Packet -> bool
    def eval(self, packet):
        return False

        
class ingress_network(Predicate):
    def __init__(self):
        self.egresses = None
        super(ingress_network,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "ingress_network"

    def set_network(self, network):
        if network == self._network:
            return 
        self._network = network
        updated_egresses = network.topology.egress_locations()
        if not self.egresses == updated_egresses:
            self.egresses = updated_egresses
    
    ### eval : Packet -> bool
    def eval(self, packet):
        switch = packet["switch"]
        port_no = packet["inport"]
        return Location(switch,port_no) in self.egresses

        
class egress_network(Predicate):
    def __init__(self):
        self.egresses = None
        super(egress_network,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "egress_network"
    
    def set_network(self, network):
        if network == self._network:
            return 
        self._network = network
        updated_egresses = network.topology.egress_locations()
        if not self.egresses == updated_egresses:
            self.egresses = updated_egresses
 
    ### eval : Packet -> bool
    def eval(self, packet):
        switch = packet["switch"]
        try:
            port_no = packet["outport"]
        except:
            return False
        return Location(switch,port_no) in self.egresses

        
class match(Predicate):
    """A set of field matches (one per field)"""
    
    ### init : List (String * FieldVal) -> List KeywordArg -> unit
    def __init__(self, *args, **kwargs):
        init_map = {}
        for (k, v) in dict(*args, **kwargs).iteritems():
            if v is not None:
                patterntype = field_patterntype(k)
                pattern_to_match = patterntype(v)
                init_map[k] = pattern_to_match
            else: 
                init_map[k] = None
        self.map = util.frozendict(init_map)
        super(match,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "match:\n%s" % util.repr_plus(self.map.items())

    ### hash : unit -> int
    def __hash__(self):
        return hash(self.map)
    
    ### eq : Predicate -> bool
    def __eq__(self, other):
        return self.map == other.map

    ### eval : Packet -> bool
    def eval(self, packet):
        for field, pattern in self.map.iteritems():
            v = packet.get_stack(field)
            if v:
                if pattern is None or not pattern.match(v[0]):
                    return False
            else:
                if pattern is not None:
                    return False
        return True

        
class union(Predicate):
    """A predicate representing the union of a list of predicates."""

    ### init : List Predicate -> unit
    def __init__(self, predicates):
        self.predicates = list(predicates)
        super(union,self).__init__()        

    ### repr : unit -> String
    def __repr__(self):
        return "union:\n%s" % util.repr_plus(self.predicates)

    def set_network(self, network):
        if network == self._network:
            return
        super(union,self).set_network(network)
        for pred in self.predicates:
            pred.set_network(network)

    def eval(self, packet):
        return any(predicate.eval(packet) for predicate in self.predicates)

        
class intersect(Predicate):
    """A predicate representing the intersection of a list of predicates."""

    def __init__(self, predicates):
        self.predicates = list(predicates)
        super(intersect,self).__init__()
        
    ### repr : unit -> String
    def __repr__(self):
        return "intersect:\n%s" % util.repr_plus(self.predicates)

    def set_network(self, network):
        if network == self._network:
            return
        super(intersect,self).set_network(network)
        for pred in self.predicates:
            pred.set_network(network)

    def eval(self, packet):
        return all(predicate.eval(packet) for predicate in self.predicates)

    def track_eval(self, packet):
        traversed = list()
        for predicate in self.predicates:
            (result,ptraversed) = predicate.track_eval(packet)
            traversed.append(ptraversed)
            if not result:
                return (False,[self,traversed])
        return (True,[self,traversed])
    
class SinglyDerivedPredicate(Predicate):
    def __init__(self, predicate):
        self.predicate = predicate
        super(SinglyDerivedPredicate,self).__init__()

    def set_network(self, network):
        if network == self._network:
            return
        super(SinglyDerivedPredicate,self).set_network(network)
        self.predicate.set_network(network)

    def eval(self, packet):
        return self.predicate.eval(packet)

    def track_eval(self,packet):
        (result,traversed) = self.predicate.track_eval(packet)
        return (result,[self,traversed])


class negate(SinglyDerivedPredicate):
    ### repr : unit -> String
    def __repr__(self):
        return "negate:\n%s" % util.repr_plus([self.predicate])

    ### eval : Packet -> bool
    def eval(self, packet):
        return not self.predicate.eval(packet)
        
    def track_eval(self,packet):
        (result,traversed) = self.predicate.track_eval(packet)
        return (not result,[self,traversed])


class difference(SinglyDerivedPredicate):
    """A predicate representing the difference of two predicates."""
    ### init : Predicate -> List Predicate -> unit
    def __init__(self,pred1,pred2):
        self.pred1 = pred1
        self.pred2 = pred1
        super(difference,self).__init__((~pred2) & pred1)
        
    ### repr : unit -> String
    def __repr__(self):
        return "difference:\n%s" % util.repr_plus([self.pred1,
                                                   self.pred2])

        
################################################################################
# Policies
################################################################################

class Policy(NetworkEvaluated):
    """Top-level abstract description of a static network program."""

    ### sub : Predicate -> Policy
    def __sub__(self, pred):
        return remove(self, pred)

    ### add : Predicate -> Policy
    def __and__(self, pred):
        return restrict(self, pred)

    ### or : Policy -> Policy
    def __or__(self, other):
        return parallel([self, other])
        
    ### rshift : Policy -> Policy
    def __rshift__(self, pol):
        return sequential([self, pol])

    ### eq : Policy -> bool
    def __eq__(self, other):
        raise NotImplementedError
        
@singleton
class passthrough(Policy):
    ### repr : unit -> String
    def __repr__(self):
        return "passthrough"
        
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        return Counter([packet])

        
@singleton
class drop(Policy):
    """Policy that drops everything."""
    ### repr : unit -> String
    def __repr__(self):
        return "drop"
        
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        return Counter()

        
class flood(Policy):
    def __init__(self):
        self.egresses = None
        self.mst = None
        super(flood,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        try: 
            return "flood on:\n%s" % self.mst
        except:
            return "flood"

    def set_network(self, network):
        if network == self._network:
            return
        if not network is None:
            updated_egresses = network.topology.egress_locations()
            if not self.egresses == updated_egresses:
                self.egresses = updated_egresses
            updated_mst = Topology.minimum_spanning_tree(network.topology)
            if not self.mst is None:
                if self.mst != updated_mst:
                    self.mst = updated_mst
            else:
                self.mst = updated_mst
        super(flood,self).set_network(network) 
        
    def eval(self, packet):
        if self.network is None:
            return Counter()
        
        switch = packet["switch"]
        inport = packet["inport"]
        if switch in self.mst:
            port_nos = {loc.port_no 
                        for loc in self.egresses if loc.switch == switch}
            for sw in self.mst.neighbors(switch):
                port_no = self.mst[switch][sw][switch]
                port_nos.add(port_no)
            try:
                if packet["outport"] == -1:
                    packets = [packet.modify(outport=port_no) \
                                   for port_no in port_nos if port_no != inport]
                else:
                    packets = [packet.push(outport=port_no) \
                                   for port_no in port_nos if port_no != inport]
            except:
                    packets = [packet.push(outport=port_no) \
                                   for port_no in port_nos if port_no != inport]
            return Counter(packets)
        else:
            return Counter()
        
        
class push(Policy):
    """push(field=value) pushes value onto header field stack"""
    ### init : List (String * FieldVal) -> List KeywordArg -> unit
    def __init__(self, *args, **kwargs):
        self.map = dict(*args, **kwargs)
        super(push,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "push:\n%s" % util.repr_plus(self.map.items())
        
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        packet = packet.pushmany(self.map)
        return Counter([packet])

        
class pop(Policy):
    """pop('field') pops value off field stack"""
    ### init : List String -> unit
    def __init__(self, *args):
        self.fields = list(args)
        super(pop,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "pop:\n%s" % util.repr_plus(self.fields)
        
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        packet = packet.popmany(self.fields)
        return Counter([packet])


class modify(Policy):
    """modify(field=value) is equivalent to
    pop('field') >> push(field=value)"""
    ### init : List (String * FieldVal) -> List KeywordArg -> unit
    def __init__(self, *args, **kwargs):
       init_map = {}
       for (k, v) in dict(*args, **kwargs).iteritems():
           if k == 'srcip' or k == 'dstip':
               init_map[k] = IP(v) 
           elif k == 'srcmac' or k == 'dstmac':
               init_map[k] = MAC(v)
           else:
               init_map[k] = v
       self.map = util.frozendict(init_map)
       super(modify,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "modify:\n%s" % util.repr_plus(self.map.items())

    ### eval : Packet -> Counter List Packet        
    def eval(self, packet):
        packet = packet.modifymany(self.map)
        return Counter([packet])


class copy(Policy):
    """copy(field1='field2') pushes the value stored at the top of 
    the field2 stack unto the field1 stack"""
    ### init : List (String * FieldVal) -> List KeywordArg -> unit
    def __init__(self, *args, **kwargs):
        self.map = dict(*args, **kwargs)
        super(copy,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "copy:\n%s" % util.repr_plus(self.map.items())
  
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        pushes = {}
        for (dstfield, srcfield) in self.map.iteritems():
            pushes[dstfield] = packet[srcfield]
        packet = packet.pushmany(pushes)
        return Counter([packet])
        
        
class move(Policy):
    """move(field1='field2') is equivalent to 
    copy(field1='field2') >> pop('field2')"""
    ### init : List (String * FieldVal) -> List KeywordArg -> unit
    def __init__(self, *args, **kwargs):
        self.map = dict(*args, **kwargs)
        super(move,self).__init__()

    ### repr : unit -> String
    def __repr__(self):
        return "move:\n%s" % util.repr_plus(self.map.items())
  
    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        pushes = {}
        pops = []
        for (dstfield, srcfield) in self.map.iteritems():
            try:
                pushes[dstfield] = packet[srcfield]
                pops.append(srcfield)
            except KeyError:
                pass
        packet = packet.pushmany(pushes).popmany(pops)
        return Counter([packet])




################################################################################
# Policy Derived from Multiple Policies
################################################################################

class MultiplyDerivedPolicy(Policy):
    ### init : List Policy -> unit
    def __init__(self, policies):
        self.policies = list(policies)
        super(MultiplyDerivedPolicy,self).__init__()

    def set_network(self, network):
        if network == self._network:
            return
        super(MultiplyDerivedPolicy,self).set_network(network)
        for policy in self.policies:
            policy.set_network(network) 

                    
class parallel(MultiplyDerivedPolicy):
    def eval(self, packet):
        c = Counter()
        for policy in self.policies:
            rc = policy.eval(packet)
            c.update(rc)
        return c

    def track_eval(self, packet):
        traversed = list()
        c = Counter()
        for policy in self.policies:
            (rc,rtraversed) = policy.track_eval(packet)
            c.update(rc)
            traversed.append(rtraversed)
        return (c,[self,traversed])
    
    ### repr : unit -> String
    def __repr__(self):
        return "parallel:\n%s" % util.repr_plus(self.policies)
        

class sequential(MultiplyDerivedPolicy):
    def eval(self, packet):
        lc = Counter([packet])
        for policy in self.policies:
            c = Counter()
            for lpacket, lcount in lc.iteritems():
                rc = policy.eval(lpacket)
                for rpacket, rcount in rc.iteritems():
                    c[rpacket] = lcount * rcount
            lc = c
        return lc

    def track_eval(self, packet):
        traversed = list()
        lc = Counter([packet])
        for policy in self.policies:
            c = Counter()
            for lpacket, lcount in lc.iteritems():
                (rc,rtraversed) = policy.track_eval(lpacket)
                traversed.append(rtraversed)
                for rpacket, rcount in rc.iteritems():
                    c[rpacket] = lcount * rcount
            lc = c
        return (lc,[self,traversed])
    
    ### repr : unit -> String
    def __repr__(self):
        return "sequential:\n%s" % util.repr_plus(self.policies)


################################################################################
# Policy Derived from a Single Policy
################################################################################
        
class SinglyDerivedPolicy(Policy):
    def __init__(self, policy):
        self.policy = policy
        super(SinglyDerivedPolicy,self).__init__()

    def set_network(self, network):
        if network == self._network:
            return
        super(SinglyDerivedPolicy,self).set_network(network)            
        if not self.policy is None:
            self.policy.set_network(network) 

    def eval(self, packet):
        return self.policy.eval(packet)

    def track_eval(self, packet):
        (result,traversed) = self.policy.track_eval(packet)
        return (result,[self,traversed])


class fwd(SinglyDerivedPolicy):
    ### init : int -> unit
    def __init__(self, outport):
        self.outport = outport
        super(fwd,self).__init__(if_(match(outport=-1),pop('outport')) 
                                 >> push(outport=self.outport))

    ### repr : unit -> String
    def __repr__(self):
        return "fwd %s" % self.outport
    

class recurse(SinglyDerivedPolicy):
    def set_network(self, network):
        if network == self.policy._network:
            return
        super(recurse,self).set_network(network)

    ### repr : unit -> String
    def __repr__(self):
        return "[recurse]:\n%s" % repr(self.policy)

class remove(SinglyDerivedPolicy):
    ### init : Policy -> Predicate -> unit
    def __init__(self, policy, predicate):
        self.predicate = predicate
        super(remove,self).__init__(policy)

    def set_network(self, network):
        if network == self._network:
            return
        super(remove,self).set_network(network)
        self.predicate.set_network(network)

    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        if not self.predicate.eval(packet):
            return self.policy.eval(packet)
        else:
            return Counter()

    def track_eval(self, packet):
        (result1,traversed1) = self.predicate.track_eval(packet)
        if not result1:
            (result2,traversed2) = self.policy.track_eval(packet)
            return (result2,[self,traversed1,traversed2])
        else:
            return (Counter(),[self,traversed1])

    ### repr : unit -> String
    def __repr__(self):
        return "remove:\n%s" % util.repr_plus([self.predicate, self.policy])

    
class restrict(SinglyDerivedPolicy):
    ### init : Policy -> Predicate -> unit
    def __init__(self, policy, predicate):
        self.predicate = predicate
        super(restrict,self).__init__(policy) 

    def set_network(self, network):
        if network == self._network:
            return
        super(restrict,self).set_network(network)
        self.predicate.set_network(network)

    ### eval : Packet -> Counter List Packet
    def eval(self, packet):
        if self.predicate.eval(packet):
            return self.policy.eval(packet)
        else:
            return Counter()

    def track_eval(self, packet):
        (result1,traversed1) = self.predicate.track_eval(packet)
        if result1:
            (result2,traversed2) = self.policy.track_eval(packet)
            return (result2,[self,traversed1,traversed2])
        else:
            return (Counter(),[self,traversed1])

    ### repr : unit -> String
    def __repr__(self):
        return "restrict:\n%s" % util.repr_plus([self.predicate,
                                                 self.policy])

class if_(SinglyDerivedPolicy):
    ### init : Predicate -> Policy -> Policy -> unit
    def __init__(self, pred, t_branch, f_branch=passthrough):
        self.pred = pred
        self.t_branch = t_branch
        self.f_branch = f_branch
        super(if_,self).__init__(self.pred[self.t_branch] | 
                                 (~self.pred)[self.f_branch])

    ### repr : unit -> String
    def __repr__(self):
        return "if\n%s\nthen\n%s\nelse\n%s" % (util.repr_plus([self.pred]),
                                               util.repr_plus([self.t_branch]),
                                               util.repr_plus([self.f_branch]))
        

class NetworkDerivedPolicy(SinglyDerivedPolicy):
    """Generates new policy every time a new network is set"""
    def __init__(self, policy_from_network):
        self.policy_from_network = policy_from_network
        super(NetworkDerivedPolicy,self).__init__(drop)

    def set_network(self, network):
        if network == self._network:
            return
        super(NetworkDerivedPolicy,self).set_network(network)
        if not network is None:
            self.policy = self.policy_from_network(network)
        else:
            self.policy = drop

    ### repr : unit -> String
    def __repr__(self):
        return "[NetworkDerivedPolicy]\n%s" % repr(self.policy)

    
def NetworkDerivedPolicyPropertyFrom(network_to_policy):
    """Makes a NetworkDerivedPolicy that is a property of a virtualization defintion 
    from a policy taking a network and returning a policy"""
    @property
    @functools.wraps(network_to_policy)
    def wrapper(self):
        return NetworkDerivedPolicy(functools.partial(network_to_policy, self))
    return wrapper


class MutablePolicy(SinglyDerivedPolicy):
    ### init : unit -> unit
    def __init__(self):
        self._policy = drop
        super(SinglyDerivedPolicy,self).__init__()
        
    @property
    def policy(self):
        return self._policy
        
    @policy.setter
    def policy(self, value):
        self._policy = value
        self._policy.set_network(self.network)

    ### repr : unit -> String
    def __repr__(self):
        return "[MutablePolicy]\n%s" % repr(self.policy)

        
# dynamic : (DecoratedPolicy ->  unit) -> DecoratedPolicy
def dynamic(fn):
    class DecoratedPolicy(MutablePolicy):
        def __init__(self, *args, **kwargs):
            # THIS CALL WORKS BY SETTING THE BEHAVIOR OF MEMBERS OF SELF.
            # IN PARICULAR, THE register_callback FUNCTION RETURNED BY self.query 
            # (ITSELF A MEMBER OF A queries_base CREATED BY self.query)
            # THIS ALLOWS FOR DECORATED POLICIES TO EVOLVE ACCORDING TO 
            # FUNCTION REGISTERED FOR CALLBACK EACH TIME A NEW EVENT OCCURS
            MutablePolicy.__init__(self)
            fn(self, *args, **kwargs)

        ### repr : unit -> String
        def __repr__(self):
            return "[DecoratedPolicy]\n%s" % repr(self.policy)
            
    # SET THE NAME OF THE DECORATED POLICY RETURNED TO BE THAT OF THE INPUT FUNCTION
    DecoratedPolicy.__name__ = fn.__name__
    return DecoratedPolicy



############################
# Query classes
############################

class FwdBucket(Policy):
    ### init : unit -> unit
    def __init__(self):
        self.listeners = []
        super(FwdBucket,self).__init__()

    ### eval : Packet -> unit
    def eval(self, packet):
        for listener in self.listeners:
            listener(packet)
        return Counter()

    ### register_callback : (Packet -> unit) -> (Packet -> unit)  
    # UNCLEAR IF THIS SIGNATURE IS OVERLY RESTRICTIVE 
    # CODE COULD PERMIT (Packet -> X) WHERE X not unit
    # CURRENT EXAMPLES USE SOLELY SIDE-EFFECTING FUNCTIONS
    def register_callback(self, fn):
        self.listeners.append(fn)
        return fn


class packets(Policy):

    class PredicateWrappedFwdBucket(Predicate):
        def __init__(self,limit=None,fields=[]):
            self.limit = limit
            self.fields = fields
            self.seen = {}
            self.fwd_bucket = FwdBucket()
            self.register_callback = self.fwd_bucket.register_callback
            super(packets.PredicateWrappedFwdBucket,self).__init__()

        def eval(self,packet):
            if not self.limit is None:
                if self.fields:    # MATCH ON PROVIDED FIELDS
                    pred = match([(field,packet[field]) for field in self.fields])
                else:              # OTHERWISE, MATCH ON ALL AVAILABLE FIELDS
                    pred = match([(field,packet[field]) 
                                  for field in packet.available_fields()])
                # INCREMENT THE NUMBER OF TIMES MATCHING PACKET SEEN
                try:
                    self.seen[pred] += 1
                except KeyError:
                    self.seen[pred] = 1

                if self.seen[pred] > self.limit:
                    return False
            self.fwd_bucket.eval(packet)
            return True
        
    def __init__(self,limit=None,fields=[]):
        self.limit = limit
        self.seen = {}
        self.fields = fields
        self.pwfb = self.PredicateWrappedFwdBucket(limit,fields)
        self.register_callback = self.pwfb.register_callback
        self.predicate = all_packets
        super(packets,self).__init__()

    def set_network(self, network):
        if network == self._network:
            return
        super(packets,self).set_network(network)
        self.pwfb.set_network(network)
        self.predicate.set_network(network)

    def eval(self,pkt):
        """Don't look any more such packets"""
        if self.predicate.eval(pkt) and not self.pwfb.eval(pkt):
            val = {h : pkt[h] for h in self.fields}
            self.predicate = ~match(val) & self.predicate
            self.predicate.set_network(self.network)
        return Counter()

    def track_eval(self,pkt):
        """Don't look any more such packets"""
        (result,traversed) = self.predicate.track_eval(pkt)
        if result:
            (result,traversed2) = self.pwfb.track_eval(pkt)
            traversed += traversed2
            if not result:
                val = {h : pkt[h] for h in self.fields}
                self.predicate = ~match(val) & self.predicate
                self.predicate.set_network(self.network)
        return (Counter(),[self,traversed])
        

class AggregateFwdBucket(FwdBucket):
    ### init : int -> List String
    def __init__(self, interval, group_by=[]):
        self.interval = interval
        self.group_by = group_by
        if group_by:
            self.aggregate = {}
        else:
            self.aggregate = 0
        import threading
        import pyretic.core.runtime
        self.query_thread = threading.Thread(target=self.report_count)
        self.query_thread.daemon = True
        self.query_thread.start()
        FwdBucket.__init__(self)

    def report_count(self):
        while(True):
            FwdBucket.eval(self, self.aggregate)
            time.sleep(self.interval)

    def aggregator(self,aggregate,pkt):
        raise NotImplementedError

    ### update : Packet -> unit
    def update_aggregate(self,pkt):
        if self.group_by:
            from pyretic.core.netcore import match
            groups = set(self.group_by) & set(pkt.available_fields())
            pred = match([(field,pkt[field]) for field in groups])
            try:
                self.aggregate[pred] = self.aggregator(self.aggregate[pred],pkt)
            except KeyError:
                self.aggregate[pred] = self.aggregator(0,pkt)
        else:
            self.aggregate = self.aggregator(self.aggregate,pkt)

    ### eval : Packet -> unit
    def eval(self, packet):
        self.update_aggregate(packet)
        return Counter()


class counts(AggregateFwdBucket):
    def aggregator(self,aggregate,pkt):
        return aggregate + 1


class sizes(AggregateFwdBucket):
    def aggregator(self,aggregate,pkt):
        return aggregate + pkt['header_len'] + pkt['payload_len']

