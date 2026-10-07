"""
simconfig.py: build Containernet simulations from a configuration file.

A simulation is described as a graph of ``nodes`` and ``links`` in YAML
(JSON works too, since JSON is valid YAML). Every field except the links
themselves is optional: whatever is left out is inferred, and every
inference is logged so the result is never a surprise. The fully resolved
graph can be written back out (``--resolved``) to pin it down or edit it.

Fidelity levels
---------------
Low - just the shape of the network. Node kinds are guessed from names,
images and degree; subnets, addresses, interface names and static routes
(with loop-free backup routes over redundant links) are generated::

    links:
      - d1 -- er1
      - er1 -- wan
      - er1 -- wan          # parallel link -> automatic backup route
      - wan -- er2
      - er2 -- d2

Medium - roles, images and a few constraints; the rest is filled in::

    defaults:
      host:   { image: ubuntu:trusty }
      router: { image: frr, cmd: /entrypoint.sh }
    nodes:
      d1: host
      d2: host
      wan_r: router
    links:
      - [d1, er1]
      - { a: er1, b: wan_r, cost: 1 }     # primary
      - { a: er1, b: wan_r, cost: 2 }     # backup
      - { a: er2, b: wan_r, delay: 20ms, loss: 1 }
      - [er2, d2]

High - everything explicit; only what is still missing (here wan_r's
routes) is generated::

    name: simple-redundancy
    addressing: { p2p_pool: 10.255.0.0/16, lan_pool: 192.168.0.0/16 }
    routing:    { mode: static, backup_routes: true }   # or mode: none
    # controllers: [ { name: onos, ip: 127.0.0.1, port: 6653 } ]
    nodes:
      d1:
        kind: host
        image: ubuntu:trusty
        gateway: 192.168.1.1
        exec: [ "echo ready" ]
      er1:
        kind: router
        image: frr
        cmd: /entrypoint.sh
        privileged: true
        sysctls: { net.ipv4.ip_forward: 1 }
        routes:
          - 192.168.2.0/24 via 10.0.1.1 metric 1
          - { to: 192.168.2.0/24, via: 20.0.1.1, metric: 2 }
      wan_r: { kind: router, image: frr, cmd: /entrypoint.sh }
    links:
      - a: { node: d1, intf: d1-eth0, ip: 192.168.1.2/24 }
        b: { node: er1, intf: er1-eth0, ip: 192.168.1.1/24 }
      - id: primary
        a: { node: er1, intf: er1-eth1, ip: 10.0.1.2/30 }
        b: { node: wan_r, intf: wan_r-eth0, ip: 10.0.1.1/30 }
        cost: 1
        delay: 10ms
        jitter: 1ms
        loss: 0.5
        bw: 100
      - id: backup
        a: { node: er1, intf: er1-eth2, ip: 20.0.1.2/30 }
        b: { node: wan_r, intf: wan_r-eth1, ip: 20.0.1.1/30 }
        cost: 2
        state: down

Reference
---------
Top level: name, defaults, addressing, routing, controllers, nodes, links.

defaults: per-kind node settings (host, router, switch) and link settings
    (link), merged underneath each node/link.

nodes: mapping name -> settings, a kind string, or nothing; or a list of
    mappings with a ``name`` key. Nodes that only appear in links are
    created automatically. Settings:
    kind      host | router | switch (inferred if omitted)
    image     Docker image (host/router)
    ip        address for the node's first interface
    gateway   default gateway (hosts)
    routes    "DEST [via IP] [dev INTF] [metric N]" strings or mappings
              with to/via/dev/metric; DEST may be "default"
    exec      command(s) run in the node after the network is up
    start     run the image's ENTRYPOINT/CMD after the network is up
    cmd, env, ports ("8080:80" or 80), volumes, privileged, sysctls,
    cpus, mem_limit, memswap_limit, cpu_shares, cpu_quota, cpu_period,
    cpuset_cpus, dns, devices, cap_add, shell (bash|sh)
              Docker options (see mininet.node.Docker)
    dpid, protocols, fail_mode
              switch options
    params    raw keyword arguments passed to addDocker/addSwitch

links: list of "a -- b" strings, [a, b] pairs or mappings with a/b (or
    endpoints: [a, b]). An endpoint is a node name or a mapping with
    node, intf and ip. Link settings: id, cost (routing weight, default 1),
    state (up|down), delay, jitter, loss, bw, max_queue_size and tc (any
    other TCIntf.config() options).

addressing: p2p_pool / p2p_prefix (router-router links, default
    10.255.0.0/16 split into /30s), lan_pool / lan_prefix (everything else,
    default 192.168.0.0/16 split into /24s). ``addressing: false`` disables
    automatic addressing.

routing: mode static (default; shortest paths with link costs, transit only
    through routers) or none; backup_routes (default true) adds loop-free
    alternates (RFC 5286) with higher metrics, so the kernel fails over
    when the primary link goes down.

controllers: OpenFlow controllers (name, ip, port). Switches become
    OpenFlow 1.3 switches when controllers exist, standalone learning
    bridges otherwise.

Usage
-----
Validate and see what would be built (no root needed)::

    python3 -m mininet.simconfig topology.yaml --dry-run --resolved out.yaml

Run it (root, opens the Mininet CLI)::

    sudo python3 -m mininet.simconfig topology.yaml

From Python::

    from mininet.simconfig import loadSimulation
    with loadSimulation( 'topology.yaml' ) as sim:
        sim.node( 'd1' ).cmd( 'ping -c1 192.168.2.2' )
        sim.link( 'primary' ).intf1.config( delay='50ms' )
"""

from __future__ import annotations

import argparse
import copy
import difflib
import heapq
import ipaddress
import json
import math
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from mininet.log import info, warn, setLogLevel

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a containernet dependency
    yaml = None

IPv4Interface = ipaddress.IPv4Interface
IPv4Network = ipaddress.IPv4Network
IPv4Address = ipaddress.IPv4Address

# Linux limits interface names to IFNAMSIZ - 1 characters
MAX_INTF_NAME_LEN = 15
DEFAULT_ROUTE = IPv4Network( '0.0.0.0/0' )


class SimConfigError( Exception ):
    """Raised for configurations that cannot be completed."""


class NodeKind( str, Enum ):
    """Role of a node in the simulated network."""

    HOST = 'host'
    ROUTER = 'router'
    SWITCH = 'switch'

    @classmethod
    def parse( cls, value: str ) -> NodeKind:
        """Parse a kind name, accepting a few common aliases.
           value: e.g. 'host', 'router', 'switch', 'server', 'bridge'
           raises: SimConfigError for unknown kinds"""
        aliases = { 'h': 'host', 'server': 'host', 'client': 'host',
                    'r': 'router', 'gateway': 'router', 'gw': 'router',
                    's': 'switch', 'bridge': 'switch', 'ovs': 'switch' }
        name = str( value ).strip().lower()
        try:
            return cls( aliases.get( name, name ) )
        except ValueError:
            raise SimConfigError( 'Unknown node kind %r (expected host, '
                                  'router or switch)' % ( value, ) )

    @property
    def isL3( self ) -> bool:
        "Whether nodes of this kind own IP addresses"
        return self is not NodeKind.SWITCH


# Inference log

@dataclass( frozen=True )
class Note( object ):
    """One inference or warning produced while completing the graph."""

    severity: str  # 'assumed' or 'warning'
    subject: str
    message: str

    def __str__( self ) -> str:
        """Human readable form, e.g. '[assumed] er1: kind router (...)'."""
        return '[%s] %s: %s' % ( self.severity, self.subject, self.message )


class InferenceLog( object ):
    """Collects (and echoes through mininet.log) everything the loader
       assumed or found suspicious."""

    def __init__( self, echo: bool = True ) -> None:
        """echo: also print notes through mininet.log"""
        self.notes: list[ Note ] = []
        self.echo = echo

    def assume( self, subject: str, message: str ) -> None:
        """Record a value that was filled in.
           subject: node, link or section the note is about
           message: what was assumed and why"""
        self._add( Note( 'assumed', subject, message ), info )

    def warning( self, subject: str, message: str ) -> None:
        """Record something that looks wrong but does not stop the build.
           subject: node, link or section the note is about
           message: description"""
        self._add( Note( 'warning', subject, message ), warn )

    def _add( self, note: Note, emit ) -> None:
        """Store a note and optionally print it."""
        self.notes.append( note )
        if self.echo:
            emit( '*** simconfig %s\n' % note )

    def report( self ) -> str:
        """Return all notes, one per line."""
        return '\n'.join( str( note ) for note in self.notes )


# Graph model

@dataclass
class Endpoint( object ):
    """One side of a link."""

    node: str
    intf: Optional[ str ] = None
    ip: Optional[ IPv4Interface ] = None
    #: False when the configured address had no /prefix
    explicitPrefix: bool = True

    def toDict( self ) -> dict:
        """Resolved configuration form."""
        result: dict = { 'node': self.node, 'intf': self.intf }
        if self.ip is not None:
            result[ 'ip' ] = str( self.ip )
        return result


@dataclass
class LinkSpec( object ):
    """A link between two endpoints."""

    index: int
    a: Endpoint
    b: Endpoint
    id: Optional[ str ] = None
    cost: float = 1.0
    state: str = 'up'
    tc: dict = field( default_factory=dict )

    @property
    def endpoints( self ) -> tuple:
        "Both endpoints, a first"
        return ( self.a, self.b )

    def label( self ) -> str:
        """Short description for log messages."""
        return self.id or 'link #%d (%s -- %s)' % ( self.index, self.a.node,
                                                     self.b.node )

    def toDict( self ) -> dict:
        """Resolved configuration form."""
        result = { 'id': self.id, 'a': self.a.toDict(), 'b': self.b.toDict(),
                   'cost': self.cost, 'state': self.state }
        for key, value in self.tc.items():
            if key in ConfigParser.TC_KEYS:
                result[ key ] = value
            else:
                result.setdefault( 'tc', {} )[ key ] = value
        return result


@dataclass
class RouteSpec( object ):
    """A static IPv4 route."""

    destination: IPv4Network
    via: Optional[ IPv4Address ] = None
    dev: Optional[ str ] = None
    metric: Optional[ int ] = None
    origin: str = 'config'

    def argv( self ) -> list[ str ]:
        """iproute2 command that installs the route."""
        dest = ( 'default' if self.destination == DEFAULT_ROUTE
                 else str( self.destination ) )
        argv = [ 'ip', 'route', 'add', dest ]
        if self.via is not None:
            argv += [ 'via', str( self.via ) ]
        if self.dev:
            argv += [ 'dev', self.dev ]
        if self.metric is not None:
            argv += [ 'metric', str( self.metric ) ]
        return argv

    def toDict( self ) -> dict:
        """Resolved configuration form."""
        result: dict = { 'to': ( 'default' if self.destination == DEFAULT_ROUTE
                                 else str( self.destination ) ) }
        for key in ( 'via', 'dev', 'metric' ):
            value = getattr( self, key )
            if value is not None:
                result[ key ] = str( value ) if key == 'via' else value
        return result


@dataclass
class NodeSpec( object ):
    """A node and its (config-level) settings."""

    name: str
    kind: Optional[ NodeKind ] = None
    image: Optional[ str ] = None
    ip: Optional[ IPv4Interface ] = None
    gateway: Optional[ IPv4Address ] = None
    routes: list = field( default_factory=list )
    commands: list = field( default_factory=list )
    start: bool = False
    #: Docker/switch options in configuration vocabulary
    options: dict = field( default_factory=dict )
    #: False for nodes that only appeared in links
    declared: bool = True

    def toDict( self ) -> dict:
        """Resolved configuration form."""
        result: dict = { 'kind': self.kind.value if self.kind else None }
        if self.image:
            result[ 'image' ] = self.image
        if self.gateway is not None:
            result[ 'gateway' ] = str( self.gateway )
        result.update( copy.deepcopy( self.options ) )
        if self.routes:
            result[ 'routes' ] = [ route.toDict() for route in self.routes ]
        if self.commands:
            result[ 'exec' ] = list( self.commands )
        if self.start:
            result[ 'start' ] = True
        return result


@dataclass
class Segment( object ):
    """A layer-2 broadcast domain: one IP subnet.
       Either a direct link between two L3 nodes or everything attached to
       a group of interconnected switches."""

    index: int
    switches: set = field( default_factory=set )
    members: list = field( default_factory=list )  # [ ( LinkSpec, Endpoint ) ]
    network: Optional[ IPv4Network ] = None

    @property
    def isPointToPoint( self ) -> bool:
        "True for a direct link between two L3 nodes"
        return not self.switches and len( self.members ) == 2

    def label( self ) -> str:
        """Short description for log messages."""
        if self.switches:
            return 'LAN %s' % '+'.join( sorted( self.switches ) )
        return 'link %s' % ' -- '.join( ep.node for _, ep in self.members )


@dataclass( frozen=True )
class Adjacency( object ):
    """L3 neighbourship over a segment, seen from ``local.node``."""

    segment: Segment
    local: Endpoint
    peer: Endpoint
    cost: float
    order: int


@dataclass
class Settings( object ):
    """Global settings of a simulation."""

    addressing: bool = True
    p2pPool: IPv4Network = IPv4Network( '10.255.0.0/16' )
    p2pPrefix: int = 30
    lanPool: IPv4Network = IPv4Network( '192.168.0.0/16' )
    lanPrefix: int = 24
    routing: str = 'static'
    backupRoutes: bool = True
    defaults: dict = field( default_factory=dict )

    def toDict( self ) -> dict:
        """Resolved configuration form."""
        return {
            'addressing': ( { 'p2p_pool': str( self.p2pPool ),
                              'p2p_prefix': self.p2pPrefix,
                              'lan_pool': str( self.lanPool ),
                              'lan_prefix': self.lanPrefix }
                            if self.addressing else False ),
            'routing': { 'mode': self.routing,
                         'backup_routes': self.backupRoutes } }


class SimGraph( object ):
    """The simulation as a graph of nodes, links and L2 segments."""

    def __init__( self, name: str = 'simulation',
                  log: Optional[ InferenceLog ] = None ) -> None:
        """name: simulation name
           log: inference log (a new one is created if omitted)"""
        self.name = name
        self.log = log or InferenceLog()
        self.settings = Settings()
        self.nodes: dict[ str, NodeSpec ] = {}
        self.links: list[ LinkSpec ] = []
        self.controllers: list[ dict ] = []
        self.segments: list[ Segment ] = []
        self._adjacency: Optional[ dict[ str, list[ Adjacency ] ] ] = None

    # Structure

    def addNode( self, spec: NodeSpec ) -> NodeSpec:
        """Add a node.
           raises: SimConfigError for duplicate names"""
        if spec.name in self.nodes:
            raise SimConfigError( 'Duplicate node %r' % spec.name )
        self.nodes[ spec.name ] = spec
        return spec

    def addLink( self, link: LinkSpec ) -> LinkSpec:
        """Add a link.
           raises: SimConfigError for self-loops"""
        if link.a.node == link.b.node:
            raise SimConfigError( '%s connects %s to itself'
                                  % ( link.label(), link.a.node ) )
        self.links.append( link )
        return link

    def kindOf( self, name: str ) -> Optional[ NodeKind ]:
        """Kind of a node (None while still unknown)."""
        return self.nodes[ name ].kind

    def linksOf( self, name: str ) -> list[ LinkSpec ]:
        """Links attached to a node, in declaration order."""
        return [ link for link in self.links
                 if name in ( link.a.node, link.b.node ) ]

    def endpointsOf( self, name: str ) -> list[ Endpoint ]:
        """Endpoints owned by a node, in declaration order."""
        return [ ep for link in self.links for ep in link.endpoints
                 if ep.node == name ]

    def neighbors( self, name: str ) -> list[ str ]:
        """Directly linked nodes (with repetitions for parallel links)."""
        return [ ( link.b if link.a.node == name else link.a ).node
                 for link in self.linksOf( name ) ]

    def degree( self, name: str ) -> int:
        """Number of links attached to a node."""
        return len( self.linksOf( name ) )

    # L3 view

    def adjacency( self ) -> dict[ str, list[ Adjacency ] ]:
        """L3 neighbours of every node via the segments (cached).
           Costs: a direct link's cost; across a switched LAN, the higher
           of the two access links' costs."""
        if self._adjacency is None:
            adjacency: dict[ str, list[ Adjacency ] ] = {
                name: [] for name in self.nodes }
            order = 0
            for segment in self.segments:
                for linkX, epX in segment.members:
                    for linkY, epY in segment.members:
                        if epX.node == epY.node:
                            continue
                        cost = ( linkX.cost if segment.isPointToPoint
                                 else max( linkX.cost, linkY.cost ) )
                        adjacency[ epX.node ].append(
                            Adjacency( segment, epX, epY, cost, order ) )
                        order += 1
            self._adjacency = adjacency
        return self._adjacency

    def distances( self, sources: dict[ str, float ] ) -> dict[ str, float ]:
        """Dijkstra from a set of nodes; only routers forward traffic.
           sources: node -> initial distance
           returns: node -> shortest distance"""
        dist = dict( sources )
        heap = [ ( d, name ) for name, d in sources.items() ]
        heapq.heapify( heap )
        adjacency = self.adjacency()
        while heap:
            d, name = heapq.heappop( heap )
            if d > dist[ name ] or self.kindOf( name ) is not NodeKind.ROUTER:
                continue
            for adj in adjacency[ name ]:
                nd = d + adj.cost
                if nd < dist.get( adj.peer.node, math.inf ):
                    dist[ adj.peer.node ] = nd
                    heapq.heappush( heap, ( nd, adj.peer.node ) )
        return dist

    # Output

    def toDict( self ) -> dict:
        """The completed graph in configuration form (round-trips through
           ConfigParser)."""
        result: dict = { 'name': self.name }
        result.update( self.settings.toDict() )
        if self.controllers:
            result[ 'controllers' ] = copy.deepcopy( self.controllers )
        result[ 'nodes' ] = { name: spec.toDict()
                              for name, spec in self.nodes.items() }
        result[ 'links' ] = [ link.toDict() for link in self.links ]
        return result

    def dumps( self ) -> str:
        """The completed graph as YAML (JSON if pyyaml is missing)."""
        data = self.toDict()
        if yaml is not None:
            return yaml.safe_dump( data, sort_keys=False )
        return json.dumps( data, indent=2 )


# Parsing

def _deepMerge( base: dict, override: dict ) -> dict:
    """Merge two mappings; nested mappings are merged, everything else in
       override wins. Neither input is modified."""
    result = copy.deepcopy( base )
    for key, value in override.items():
        if isinstance( value, dict ) and isinstance( result.get( key ), dict ):
            result[ key ] = _deepMerge( result[ key ], value )
        else:
            result[ key ] = copy.deepcopy( value )
    return result


class ConfigParser( object ):
    """Turns a configuration mapping (or file) into a raw SimGraph."""

    TOP_KEYS = ( 'name', 'defaults', 'addressing', 'routing', 'controllers',
                 'nodes', 'links' )
    DOCKER_KEYS = ( 'cmd', 'env', 'environment', 'ports', 'volumes',
                    'privileged', 'sysctls', 'cpus', 'mem_limit',
                    'memswap_limit', 'cpu_shares', 'cpu_quota', 'cpu_period',
                    'cpuset_cpus', 'dns', 'devices', 'cap_add', 'shell' )
    SWITCH_KEYS = ( 'dpid', 'protocols', 'fail_mode' )
    NODE_KEYS = ( ( 'name', 'kind', 'image', 'ip', 'gateway', 'routes', 'exec',
                    'start', 'params' ) + DOCKER_KEYS + SWITCH_KEYS )
    TC_KEYS = ( 'delay', 'jitter', 'loss', 'bw', 'max_queue_size' )
    LINK_KEYS = ( ( 'id', 'a', 'b', 'endpoints', 'cost', 'state', 'tc' )
                  + TC_KEYS )
    ENDPOINT_KEYS = ( 'node', 'intf', 'ip' )
    _linkArrow = re.compile( r'\s*(?:<-+>|-{2,}|->)\s*' )
    _routeRegex = re.compile(
        r'^(?P<to>\S+)(?:\s+via\s+(?P<via>\S+))?(?:\s+dev\s+(?P<dev>\S+))?'
        r'(?:\s+metric\s+(?P<metric>\d+))?$' )
    _nameRegex = re.compile( r'^[A-Za-z0-9][A-Za-z0-9_.-]*$' )

    def __init__( self, log: InferenceLog ) -> None:
        """log: where warnings about the configuration go"""
        self.log = log

    # Entry points

    @staticmethod
    def readFile( path: Union[ str, Path ] ) -> dict:
        """Read a YAML or JSON configuration file.
           raises: SimConfigError if the file is not a mapping"""
        text = Path( path ).read_text()
        if yaml is not None:
            data = yaml.safe_load( text )
        else:
            data = json.loads( text )
        if not isinstance( data, dict ):
            raise SimConfigError( '%s: expected a mapping at the top level'
                                  % path )
        return data

    def parse( self, data: dict ) -> SimGraph:
        """Parse a configuration mapping into a SimGraph.
           data: configuration (see module documentation)"""
        self._checkKeys( data, self.TOP_KEYS, 'configuration' )
        graph = SimGraph( str( data.get( 'name' ) or 'simulation' ), self.log )
        self._parseDefaults( graph, data.get( 'defaults' ) or {} )
        self._parseAddressing( graph, data.get( 'addressing', True ) )
        self._parseRouting( graph, data.get( 'routing' ) or {} )
        graph.controllers = self._parseControllers(
            data.get( 'controllers' ) or [] )
        for name, spec in self._iterNodes( data.get( 'nodes' ) or {} ):
            graph.addNode( self._parseNode( name, spec ) )
        links = data.get( 'links' ) or []
        if not isinstance( links, list ):
            raise SimConfigError( "'links' must be a list" )
        for index, item in enumerate( links ):
            graph.addLink( self._parseLink( index, item, graph ) )
        if not graph.links:
            self.log.warning( 'links', 'no links defined' )
        return graph

    # Helpers

    def _checkKeys( self, mapping: dict, allowed: Iterable[ str ],
                    where: str ) -> None:
        """Warn about unknown keys, suggesting close matches (typos)."""
        allowed = list( allowed )
        for key in mapping:
            if key not in allowed:
                hint = difflib.get_close_matches( str( key ), allowed, n=1 )
                self.log.warning( where, 'unknown key %r ignored%s' % (
                    key, ' (did you mean %r?)' % hint[ 0 ] if hint else '' ) )

    @staticmethod
    def _interface( value: Any, where: str ) -> tuple:
        """Parse an interface address.
           returns: ( IPv4Interface, explicitPrefix )"""
        text = str( value ).strip()
        try:
            return IPv4Interface( text ), '/' in text
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 address %r'
                                  % ( where, value ) )

    @staticmethod
    def _address( value: Any, where: str ) -> IPv4Address:
        """Parse a plain IPv4 address."""
        try:
            return IPv4Address( str( value ).strip() )
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 address %r'
                                  % ( where, value ) )

    @staticmethod
    def _network( value: Any, where: str ) -> IPv4Network:
        """Parse a network ('default' means 0.0.0.0/0)."""
        text = str( value ).strip()
        if text == 'default':
            return DEFAULT_ROUTE
        try:
            return IPv4Network( text, strict=False )
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 network %r'
                                  % ( where, value ) )

    def _parseDefaults( self, graph: SimGraph, data: dict ) -> None:
        """Parse the per-kind defaults section."""
        if not isinstance( data, dict ):
            raise SimConfigError( "'defaults' must be a mapping" )
        allowed = [ kind.value for kind in NodeKind ] + [ 'link' ]
        self._checkKeys( data, allowed, 'defaults' )
        for key in allowed:
            section = data.get( key ) or {}
            if not isinstance( section, dict ):
                raise SimConfigError( "defaults.%s must be a mapping" % key )
            keys = self.LINK_KEYS if key == 'link' else self.NODE_KEYS
            self._checkKeys( section, keys, 'defaults.%s' % key )
            graph.settings.defaults[ key ] = section

    def _parseAddressing( self, graph: SimGraph, data: Any ) -> None:
        """Parse the addressing section (or a boolean)."""
        settings = graph.settings
        if isinstance( data, bool ):
            settings.addressing = data
            return
        if not isinstance( data, dict ):
            raise SimConfigError( "'addressing' must be a mapping or boolean" )
        self._checkKeys( data, ( 'enabled', 'p2p_pool', 'p2p_prefix',
                                 'lan_pool', 'lan_prefix' ), 'addressing' )
        settings.addressing = bool( data.get( 'enabled', True ) )
        if 'p2p_pool' in data:
            settings.p2pPool = self._network( data[ 'p2p_pool' ], 'addressing' )
        if 'lan_pool' in data:
            settings.lanPool = self._network( data[ 'lan_pool' ], 'addressing' )
        settings.p2pPrefix = int( data.get( 'p2p_prefix', settings.p2pPrefix ) )
        settings.lanPrefix = int( data.get( 'lan_prefix', settings.lanPrefix ) )
        for pool, prefix, label in (
                ( settings.p2pPool, settings.p2pPrefix, 'p2p' ),
                ( settings.lanPool, settings.lanPrefix, 'lan' ) ):
            if not pool.prefixlen <= prefix <= 30:
                raise SimConfigError( 'addressing: %s_prefix /%d does not fit '
                                      'in %s' % ( label, prefix, pool ) )

    def _parseRouting( self, graph: SimGraph, data: Any ) -> None:
        """Parse the routing section (a mapping or just the mode)."""
        if isinstance( data, str ):
            data = { 'mode': data }
        if not isinstance( data, dict ):
            raise SimConfigError( "'routing' must be a mapping or a mode" )
        self._checkKeys( data, ( 'mode', 'backup_routes' ), 'routing' )
        mode = str( data.get( 'mode', 'static' ) ).lower()
        if mode not in ( 'static', 'none' ):
            raise SimConfigError( "routing.mode must be 'static' or 'none'" )
        graph.settings.routing = mode
        graph.settings.backupRoutes = bool( data.get( 'backup_routes', True ) )

    def _parseControllers( self, data: Any ) -> list[ dict ]:
        """Parse the controllers list."""
        if isinstance( data, ( str, dict ) ):
            data = [ data ]
        controllers = []
        for index, item in enumerate( data ):
            if isinstance( item, str ):
                item = { 'name': item }
            self._checkKeys( item, ( 'name', 'ip', 'port' ),
                             'controllers[%d]' % index )
            controllers.append( {
                'name': str( item.get( 'name' ) or 'c%d' % index ),
                'ip': str( item.get( 'ip', '127.0.0.1' ) ),
                'port': int( item.get( 'port', 6653 ) ) } )
        return controllers

    def _iterNodes( self, data: Any ) -> Iterator[ tuple ]:
        """Yield ( name, settings ) from the mapping or list form."""
        if isinstance( data, dict ):
            yield from data.items()
        elif isinstance( data, list ):
            for index, item in enumerate( data ):
                if isinstance( item, str ):
                    yield item, None
                elif isinstance( item, dict ) and 'name' in item:
                    yield item[ 'name' ], item
                else:
                    raise SimConfigError( 'nodes[%d]: expected a name or a '
                                          'mapping with "name"' % index )
        else:
            raise SimConfigError( "'nodes' must be a mapping or a list" )

    def _parseNode( self, name: Any, data: Any ) -> NodeSpec:
        """Parse one node's settings (mapping, kind string or None)."""
        name = str( name )
        if not self._nameRegex.match( name ):
            raise SimConfigError( 'Invalid node name %r (letters, digits, '
                                  '"_", "." and "-" only)' % name )
        if data is None:
            data = {}
        elif isinstance( data, str ):
            data = { 'kind': data }
        elif not isinstance( data, dict ):
            raise SimConfigError( 'node %s: expected a mapping' % name )
        self._checkKeys( data, self.NODE_KEYS, 'node %s' % name )
        spec = NodeSpec( name )
        if data.get( 'kind' ) is not None:
            spec.kind = NodeKind.parse( data[ 'kind' ] )
        spec.image = data.get( 'image' )
        if data.get( 'ip' ) is not None:
            spec.ip, explicit = self._interface( data[ 'ip' ], 'node %s' % name )
            if not explicit:
                spec.ip = IPv4Interface( '%s/24' % spec.ip.ip )
                self.log.assume( name, 'ip %s has no prefix; using /24'
                                 % spec.ip.ip )
        if data.get( 'gateway' ) is not None:
            spec.gateway = self._address( data[ 'gateway' ], 'node %s' % name )
        spec.routes = [ self._parseRoute( route, name )
                        for route in data.get( 'routes' ) or [] ]
        commands = data.get( 'exec' ) or []
        spec.commands = [ commands ] if isinstance( commands, str ) else [
            str( command ) for command in commands ]
        spec.start = bool( data.get( 'start', False ) )
        spec.options = { key: copy.deepcopy( value )
                         for key, value in data.items()
                         if key in self.DOCKER_KEYS + self.SWITCH_KEYS
                         + ( 'params', ) }
        return spec

    def _parseRoute( self, data: Any, node: str ) -> RouteSpec:
        """Parse a route string or mapping."""
        where = 'node %s route' % node
        if isinstance( data, str ):
            match = self._routeRegex.match( data.strip() )
            if not match:
                raise SimConfigError( '%s: cannot parse %r (expected "DEST '
                                      '[via IP] [dev INTF] [metric N]")'
                                      % ( where, data ) )
            data = { key: value for key, value in match.groupdict().items()
                     if value is not None }
        if not isinstance( data, dict ):
            raise SimConfigError( '%s: expected a string or mapping' % where )
        self._checkKeys( data, ( 'to', 'destination', 'via', 'dev', 'metric' ),
                         where )
        destination = data.get( 'to', data.get( 'destination' ) )
        if destination is None:
            raise SimConfigError( '%s: missing destination ("to")' % where )
        return RouteSpec(
            destination=self._network( destination, where ),
            via=( self._address( data[ 'via' ], where )
                  if data.get( 'via' ) is not None else None ),
            dev=data.get( 'dev' ),
            metric=( int( data[ 'metric' ] )
                     if data.get( 'metric' ) is not None else None ) )

    def _parseEndpoint( self, data: Any, where: str ) -> Endpoint:
        """Parse a node name or endpoint mapping."""
        if isinstance( data, str ):
            return Endpoint( data.strip() )
        if not isinstance( data, dict ) or not data.get( 'node' ):
            raise SimConfigError( '%s: endpoint must be a node name or a '
                                  'mapping with "node"' % where )
        self._checkKeys( data, self.ENDPOINT_KEYS, where )
        endpoint = Endpoint( str( data[ 'node' ] ), data.get( 'intf' ) )
        if data.get( 'ip' ) is not None:
            endpoint.ip, endpoint.explicitPrefix = self._interface(
                data[ 'ip' ], where )
        return endpoint

    def _parseLink( self, index: int, data: Any, graph: SimGraph ) -> LinkSpec:
        """Parse one link in string, pair or mapping form."""
        where = 'links[%d]' % index
        if isinstance( data, str ):
            parts = self._linkArrow.split( data.strip() )
            if len( parts ) != 2 or not all( parts ):
                raise SimConfigError( '%s: expected "a -- b", got %r'
                                      % ( where, data ) )
            data = { 'a': parts[ 0 ], 'b': parts[ 1 ] }
        elif isinstance( data, list ):
            if len( data ) != 2:
                raise SimConfigError( '%s: a link needs exactly two '
                                      'endpoints' % where )
            data = { 'a': data[ 0 ], 'b': data[ 1 ] }
        elif not isinstance( data, dict ):
            raise SimConfigError( '%s: unsupported link format' % where )
        data = _deepMerge( graph.settings.defaults.get( 'link', {} ), data )
        self._checkKeys( data, self.LINK_KEYS, where )
        if 'endpoints' in data:
            pair = data[ 'endpoints' ]
            if not isinstance( pair, list ) or len( pair ) != 2:
                raise SimConfigError( '%s: endpoints must list two '
                                      'endpoints' % where )
            data.setdefault( 'a', pair[ 0 ] )
            data.setdefault( 'b', pair[ 1 ] )
        if 'a' not in data or 'b' not in data:
            raise SimConfigError( '%s: missing endpoint "a" or "b"' % where )
        state = str( data.get( 'state', 'up' ) ).lower()
        if state not in ( 'up', 'down' ):
            raise SimConfigError( "%s: state must be 'up' or 'down'" % where )
        tc = dict( data.get( 'tc' ) or {} )
        tc.update( { key: data[ key ] for key in self.TC_KEYS if key in data } )
        cost = float( data.get( 'cost', 1 ) )
        cost = int( cost ) if cost.is_integer() else cost
        if cost <= 0:
            raise SimConfigError( '%s: cost must be positive' % where )
        return LinkSpec( index=index,
                         a=self._parseEndpoint( data[ 'a' ], where + '.a' ),
                         b=self._parseEndpoint( data[ 'b' ], where + '.b' ),
                         id=data.get( 'id' ), cost=cost, state=state, tc=tc )


# Completion passes

class CompletionPass( ABC ):
    """One step that fills in missing information on a SimGraph."""

    @abstractmethod
    def apply( self, graph: SimGraph ) -> None:
        """Complete the graph in place.
           graph: graph to complete"""


class NodeDiscovery( CompletionPass ):
    """Creates nodes that only appear in links; flags isolated nodes."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        for link in graph.links:
            for endpoint in link.endpoints:
                if endpoint.node not in graph.nodes:
                    graph.addNode( NodeSpec( endpoint.node, declared=False ) )
                    graph.log.assume( endpoint.node,
                                      'not declared; created from %s'
                                      % link.label() )
        for name in graph.nodes:
            if graph.degree( name ) == 0:
                graph.log.warning( name, 'has no links' )


class KindInference( CompletionPass ):
    """Guesses missing node kinds: image, then name, then degree."""

    ROUTER_IMAGE = re.compile( r'frr|quagga|bird|vyos|gobgp|openwrt|routeros' )
    SWITCH_IMAGE = re.compile( r'openvswitch|\bovs\b' )
    ROUTER_TOKENS = frozenset( [ 'r', 'rt', 'rtr', 'router', 'gw', 'gateway',
                                 'er', 'cr', 'pe', 'ce', 'wan', 'core', 'edge',
                                 'isp', 'fw', 'firewall', 'bgp' ] )
    SWITCH_TOKENS = frozenset( [ 's', 'sw', 'switch', 'bridge', 'tor', 'ovs' ] )
    HOST_TOKENS = frozenset( [ 'h', 'host', 'd', 'pc', 'client', 'server',
                               'srv', 'vm', 'user', 'ue', 'web', 'db', 'cam',
                               'sensor', 'node' ] )

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        for spec in graph.nodes.values():
            if spec.kind is None:
                spec.kind, reason = self.infer( spec, graph )
                graph.log.assume( spec.name, 'kind %s (%s)'
                                  % ( spec.kind.value, reason ) )

    def infer( self, spec: NodeSpec, graph: SimGraph ) -> tuple:
        """Return ( NodeKind, reason ) for a node without a kind."""
        image = ( spec.image or '' ).lower()
        if self.ROUTER_IMAGE.search( image ):
            return NodeKind.ROUTER, 'image %s' % spec.image
        if self.SWITCH_IMAGE.search( image ):
            return NodeKind.SWITCH, 'image %s' % spec.image
        tokens = [ token for token in re.split( r'[^a-z]+', spec.name.lower() )
                   if token ]
        # The last role-like word wins: 'video_server', 'wan_r', 'web_gw'
        for token in reversed( tokens ):
            for kind, words in ( ( NodeKind.ROUTER, self.ROUTER_TOKENS ),
                                 ( NodeKind.SWITCH, self.SWITCH_TOKENS ),
                                 ( NodeKind.HOST, self.HOST_TOKENS ) ):
                if token in words:
                    return kind, 'name contains %r' % token
        degree = graph.degree( spec.name )
        if degree >= 2:
            return NodeKind.ROUTER, '%d links' % degree
        return NodeKind.HOST, '%d link' % degree


class NodeDefaults( CompletionPass ):
    """Applies per-kind defaults and built-in requirements."""

    BUILTIN = {
        NodeKind.HOST: { 'image': 'alpine:3.20' },
        NodeKind.ROUTER: {
            'image': 'alpine:3.20',
            'privileged': True,
            # forwarding, and no reverse-path filtering so backup paths work
            'sysctls': { 'net.ipv4.ip_forward': 1,
                         'net.ipv4.conf.all.rp_filter': 0,
                         'net.ipv4.conf.default.rp_filter': 0 } },
        NodeKind.SWITCH: {},
    }

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        for spec in graph.nodes.values():
            configured = graph.settings.defaults.get( spec.kind.value, {} )
            builtin = self.BUILTIN[ spec.kind ]
            if spec.image is None and spec.kind.isL3:
                if configured.get( 'image' ):
                    spec.image = configured[ 'image' ]
                else:
                    spec.image = builtin[ 'image' ]
                    graph.log.assume( spec.name, 'image %s (built-in default '
                                      'for %ss)' % ( spec.image,
                                                     spec.kind.value ) )
            merged = _deepMerge( { k: v for k, v in builtin.items()
                                   if k != 'image' }, configured )
            for key in ( 'kind', 'name', 'image' ):
                merged.pop( key, None )
            for key in ( 'routes', 'exec', 'start', 'ip', 'gateway' ):
                value = merged.pop( key, None )
                if value is not None and key == 'start' and not spec.start:
                    spec.start = bool( value )
                elif value is not None and key == 'exec' and not spec.commands:
                    spec.commands = [ value ] if isinstance( value, str ) \
                        else list( value )
            spec.options = _deepMerge( merged, spec.options )
            if spec.kind is NodeKind.SWITCH:
                self._checkSwitch( spec, graph )
            if spec.kind is NodeKind.ROUTER and not spec.options.get(
                    'sysctls', {} ).get( 'net.ipv4.ip_forward' ):
                graph.log.warning( spec.name, 'router without '
                                   'net.ipv4.ip_forward=1 will not forward' )

    @staticmethod
    def _checkSwitch( spec: NodeSpec, graph: SimGraph ) -> None:
        """Warn about container settings on switches."""
        dockerOnly = [ key for key in spec.options
                       if key in ConfigParser.DOCKER_KEYS ]
        if spec.image or dockerOnly:
            graph.log.warning( spec.name, 'switches are Open vSwitch bridges; '
                               'ignoring %s' % ', '.join(
                                   ( [ 'image' ] if spec.image else [] )
                                   + dockerOnly ) )
            spec.image = None
            for key in dockerOnly:
                spec.options.pop( key )


class SwitchIdentity( CompletionPass ):
    """Gives every switch a unique datapath ID."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        switches = [ spec for spec in graph.nodes.values()
                     if spec.kind is NodeKind.SWITCH ]
        used = { int( str( spec.options[ 'dpid' ] ).replace( ':', '' ), 16 )
                 for spec in switches if spec.options.get( 'dpid' ) }
        nextId = 1
        for spec in switches:
            if spec.options.get( 'dpid' ):
                continue
            digits = re.findall( r'\d+', spec.name )
            dpid = int( digits[ 0 ] ) if digits else None
            if dpid is None or dpid in used or dpid == 0:
                while nextId in used:
                    nextId += 1
                reason = ( 'name has no number' if dpid is None
                           else 'dpid %d already used' % dpid )
                dpid = nextId
                graph.log.assume( spec.name, 'dpid %d (%s)' % ( dpid, reason ) )
            used.add( dpid )
            spec.options[ 'dpid' ] = '%016x' % dpid


class InterfaceNaming( CompletionPass ):
    """Names every interface <node>-eth<N>, unique and <= 15 characters."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        taken: set = set()
        for link in graph.links:
            for endpoint in link.endpoints:
                if endpoint.intf:
                    if endpoint.intf in taken:
                        raise SimConfigError( 'interface %s is used twice'
                                              % endpoint.intf )
                    if len( endpoint.intf ) > MAX_INTF_NAME_LEN:
                        raise SimConfigError( 'interface name %s is longer than '
                                              '%d characters' % (
                                                  endpoint.intf,
                                                  MAX_INTF_NAME_LEN ) )
                    taken.add( endpoint.intf )
        counters: dict[ str, int ] = {}
        for link in graph.links:
            for endpoint in link.endpoints:
                if endpoint.intf:
                    continue
                kind = graph.kindOf( endpoint.node )
                start = 1 if kind is NodeKind.SWITCH else 0
                number = counters.get( endpoint.node, start )
                name = self._name( endpoint.node, number )
                while name in taken:
                    number += 1
                    name = self._name( endpoint.node, number )
                counters[ endpoint.node ] = number + 1
                if not name.startswith( endpoint.node + '-' ):
                    graph.log.assume( endpoint.node, 'interface %s (node name '
                                      'too long for %s-eth%d)'
                                      % ( name, endpoint.node, number ) )
                endpoint.intf = name
                taken.add( name )

    @staticmethod
    def _name( node: str, number: int ) -> str:
        """Canonical name, shortened to the kernel limit if needed."""
        suffix = '-eth%d' % number
        if len( node ) + len( suffix ) <= MAX_INTF_NAME_LEN:
            return node + suffix
        return node[ :MAX_INTF_NAME_LEN - len( suffix ) + 1 ] + 'e%d' % number


class LinkIdentity( CompletionPass ):
    """Gives every link a unique id '<intfA>-<intfB>' (the name used by
       NetworkDegradationSimulation's web UI)."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        seen: set = set()
        for link in graph.links:
            if not link.id:
                link.id = '%s-%s' % ( link.a.intf, link.b.intf )
            if link.id in seen:
                raise SimConfigError( 'duplicate link id %s' % link.id )
            seen.add( link.id )


class SegmentDiscovery( CompletionPass ):
    """Groups interfaces into L2 segments (one subnet each)."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        parent = { name: name for name, spec in graph.nodes.items()
                   if spec.kind is NodeKind.SWITCH }

        def root( name: str ) -> str:
            """Union-find root with path halving."""
            while parent[ name ] != name:
                parent[ name ] = parent[ parent[ name ] ]
                name = parent[ name ]
            return name

        for link in graph.links:
            if link.a.node in parent and link.b.node in parent:
                parent[ root( link.a.node ) ] = root( link.b.node )
        lans: dict[ str, Segment ] = {}
        graph.segments = []

        def lanOf( switch: str ) -> Segment:
            """Segment of a switch domain (created on first use)."""
            key = root( switch )
            if key not in lans:
                lans[ key ] = Segment( len( graph.segments ) )
                graph.segments.append( lans[ key ] )
            lans[ key ].switches.add( switch )
            return lans[ key ]

        for link in graph.links:
            switchA = link.a.node in parent
            switchB = link.b.node in parent
            if switchA and switchB:
                lanOf( link.a.node )
                lanOf( link.b.node )
            elif switchA or switchB:
                switch, host = ( ( link.a, link.b ) if switchA
                                 else ( link.b, link.a ) )
                if switch.ip is not None:
                    graph.log.warning( switch.intf, 'switch ports have no IP '
                                       'address; ignoring %s' % switch.ip )
                    switch.ip = None
                lanOf( switch.node ).members.append( ( link, host ) )
            else:
                segment = Segment( len( graph.segments ),
                                   members=[ ( link, link.a ),
                                             ( link, link.b ) ] )
                graph.segments.append( segment )
        graph._adjacency = None


class AddressPlanning( CompletionPass ):
    """Fills in subnets and addresses for every L3 interface."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        self._applyNodeHints( graph )
        self._completePrefixes( graph )
        used = self._explicitNetworks( graph )
        if not graph.settings.addressing:
            missing = [ ep for segment in graph.segments
                        for _, ep in segment.members if ep.ip is None ]
            if missing:
                graph.log.assume( 'addressing', 'disabled; %d interfaces stay '
                                  'without an address' % len( missing ) )
            return
        for segment in graph.segments:
            if segment.network is None and segment.members:
                segment.network = self._allocate( segment, graph, used )
                used.append( segment.network )
            if segment.network is not None:
                self._assignHosts( segment, graph )

    @staticmethod
    def _applyNodeHints( graph: SimGraph ) -> None:
        """A node-level 'ip' addresses its first unaddressed interface."""
        for spec in graph.nodes.values():
            if spec.ip is None:
                continue
            for endpoint in graph.endpointsOf( spec.name ):
                if endpoint.ip is None:
                    endpoint.ip = spec.ip
                    graph.log.assume( spec.name, 'node ip %s assigned to %s'
                                      % ( spec.ip, endpoint.intf ) )
                    break
            else:
                graph.log.warning( spec.name, 'node ip %s unused: every '
                                   'interface has an address' % spec.ip )

    @staticmethod
    def _completePrefixes( graph: SimGraph ) -> None:
        """Give addresses written without /prefix their segment's prefix."""
        for segment in graph.segments:
            known = [ ep.ip.network for _, ep in segment.members
                      if ep.ip is not None and ep.explicitPrefix ]
            for _, endpoint in segment.members:
                if endpoint.ip is None or endpoint.explicitPrefix:
                    continue
                prefix = known[ 0 ].prefixlen if known else 24
                endpoint.ip = IPv4Interface( '%s/%d' % ( endpoint.ip.ip,
                                                         prefix ) )
                endpoint.explicitPrefix = True
                graph.log.assume( endpoint.intf, 'address %s (prefix %s)' % (
                    endpoint.ip, 'from %s' % segment.label() if known
                    else 'assumed /24' ) )

    @staticmethod
    def _explicitNetworks( graph: SimGraph ) -> list:
        """Derive segment networks from configured addresses."""
        used = []
        for segment in graph.segments:
            addressed = [ ep for _, ep in segment.members if ep.ip is not None ]
            if not addressed:
                continue
            segment.network = addressed[ 0 ].ip.network
            used.append( segment.network )
            for endpoint in addressed[ 1: ]:
                if endpoint.ip.network != segment.network:
                    graph.log.warning( endpoint.intf, '%s is not in %s like '
                                       'the rest of %s' % (
                                           endpoint.ip, segment.network,
                                           segment.label() ) )
        return used

    @staticmethod
    def _allocate( segment: Segment, graph: SimGraph,
                   used: list ) -> IPv4Network:
        """Pick a free subnet for a segment from the matching pool."""
        settings = graph.settings
        routersOnly = all( graph.kindOf( ep.node ) is NodeKind.ROUTER
                           for _, ep in segment.members )
        if segment.isPointToPoint and routersOnly:
            pool, prefix, label = settings.p2pPool, settings.p2pPrefix, 'p2p'
        else:
            pool, prefix, label = settings.lanPool, settings.lanPrefix, 'lan'
            # grow the subnet if the LAN has more interfaces than fit
            needed = max( 32 - math.ceil( math.log2(
                len( segment.members ) + 2 ) ), pool.prefixlen )
            prefix = min( prefix, needed )
        for candidate in pool.subnets( new_prefix=prefix ):
            if not any( candidate.overlaps( net ) for net in used ):
                graph.log.assume( segment.label(), 'subnet %s (%s pool)'
                                  % ( candidate, label ) )
                return candidate
        raise SimConfigError( '%s pool %s is exhausted' % ( label, pool ) )

    @staticmethod
    def _assignHosts( segment: Segment, graph: SimGraph ) -> None:
        """Address unaddressed members: routers first (gateways get the
           lowest addresses), then hosts, in declaration order."""
        taken = { ep.ip.ip for _, ep in segment.members if ep.ip is not None }
        free = ( address for address in segment.network.hosts()
                 if address not in taken )
        pending = sorted( ( ep for _, ep in segment.members if ep.ip is None ),
                          key=lambda ep: graph.kindOf( ep.node )
                          is not NodeKind.ROUTER )
        for endpoint in pending:
            try:
                address = next( free )
            except StopIteration:
                raise SimConfigError( '%s: subnet %s is full'
                                      % ( segment.label(), segment.network ) )
            endpoint.ip = IPv4Interface( '%s/%d' % (
                address, segment.network.prefixlen ) )
            graph.log.assume( endpoint.intf, 'address %s' % endpoint.ip )


class RoutePlanning( CompletionPass ):
    """Generates static routes along shortest paths, plus loop-free
       alternates (RFC 5286) as higher-metric backups."""

    def apply( self, graph: SimGraph ) -> None:
        """See CompletionPass.apply."""
        if graph.settings.routing != 'static':
            graph.log.assume( 'routing', 'mode %s; only configured routes '
                              'are installed' % graph.settings.routing )
            return
        self._betweenCache: dict[ str, dict[ str, float ] ] = {}
        for spec in graph.nodes.values():
            if spec.kind is NodeKind.HOST:
                self._planHost( spec, graph )
        for segment in graph.segments:
            if segment.network is None:
                continue
            routers = { ep.node: 0.0 for _, ep in segment.members
                        if graph.kindOf( ep.node ) is NodeKind.ROUTER }
            distToSegment = graph.distances( routers )
            members = { ep.node for _, ep in segment.members }
            for spec in graph.nodes.values():
                if ( spec.kind is NodeKind.ROUTER and spec.name not in members
                     and not self._hasRoute( spec, segment.network ) ):
                    self._planRouter( spec, segment, distToSegment, graph )
        for spec in graph.nodes.values():
            generated = [ r for r in spec.routes if r.origin == 'auto' ]
            if generated:
                graph.log.assume( spec.name, '%d static route(s) generated'
                                  % len( generated ) )

    @staticmethod
    def _hasRoute( spec: NodeSpec, network: IPv4Network ) -> bool:
        """Whether the node already has a configured route to network."""
        return any( route.destination == network for route in spec.routes )

    def _between( self, source: str, graph: SimGraph ) -> dict[ str, float ]:
        """Cached router-transit distances from one node."""
        if source not in self._betweenCache:
            self._betweenCache[ source ] = graph.distances( { source: 0.0 } )
        return self._betweenCache[ source ]

    def _planHost( self, spec: NodeSpec, graph: SimGraph ) -> None:
        """Default route(s) for a host via its gateway router(s)."""
        if self._hasRoute( spec, DEFAULT_ROUTE ):
            return
        if spec.gateway is not None:
            spec.routes.append( RouteSpec( DEFAULT_ROUTE, spec.gateway,
                                           origin='auto' ) )
            return
        gateways = sorted( ( adj for adj in graph.adjacency()[ spec.name ]
                             if graph.kindOf( adj.peer.node ) is NodeKind.ROUTER
                             and adj.peer.ip is not None ),
                           key=lambda adj: ( adj.cost, adj.order ) )
        if not graph.settings.backupRoutes:
            gateways = gateways[ :1 ]
        for metric, adj in enumerate( gateways, start=1 ):
            spec.routes.append( RouteSpec( DEFAULT_ROUTE, adj.peer.ip.ip,
                                           adj.local.intf, metric, 'auto' ) )
        if gateways:
            spec.gateway = gateways[ 0 ].peer.ip.ip
            graph.log.assume( spec.name, 'gateway %s (%s)'
                              % ( spec.gateway, gateways[ 0 ].peer.node ) )

    def _planRouter( self, spec: NodeSpec, segment: Segment,
                     distToSegment: dict, graph: SimGraph ) -> None:
        """Routes from one router to one remote segment."""
        if spec.name not in distToSegment:
            graph.log.warning( spec.name, 'no path to %s (%s)' % (
                segment.network, segment.label() ) )
            return
        own = distToSegment[ spec.name ]
        candidates = []
        for adj in graph.adjacency()[ spec.name ]:
            peer = adj.peer.node
            if ( graph.kindOf( peer ) is not NodeKind.ROUTER
                 or peer not in distToSegment or adj.peer.ip is None ):
                continue
            total = adj.cost + distToSegment[ peer ]
            loopFree = distToSegment[ peer ] < (
                self._between( peer, graph ).get( spec.name, math.inf ) + own )
            candidates.append( ( total, adj.order, adj, loopFree ) )
        candidates.sort( key=lambda c: ( c[ 0 ], c[ 1 ] ) )
        chosen = candidates[ :1 ]
        if graph.settings.backupRoutes:
            chosen += [ c for c in candidates[ 1: ] if c[ 3 ] ]
        for metric, ( _total, _order, adj, _lfa ) in enumerate( chosen, 1 ):
            spec.routes.append( RouteSpec( segment.network, adj.peer.ip.ip,
                                           adj.local.intf, metric, 'auto' ) )


class GraphCompleter( object ):
    """Runs the completion passes in order. Add or replace passes to change
       how blanks are filled in."""

    DEFAULT_PASSES = ( NodeDiscovery, KindInference, NodeDefaults,
                       SwitchIdentity, InterfaceNaming, LinkIdentity,
                       SegmentDiscovery, AddressPlanning, RoutePlanning )

    def __init__( self, passes: Optional[ Iterable[ CompletionPass ] ] = None
                  ) -> None:
        """passes: pass instances to run (default: DEFAULT_PASSES)"""
        self.passes = ( list( passes ) if passes is not None
                        else [ cls() for cls in self.DEFAULT_PASSES ] )

    def complete( self, graph: SimGraph ) -> SimGraph:
        """Fill in every blank in the graph (in place) and return it."""
        for completionPass in self.passes:
            completionPass.apply( graph )
        return graph


# Building and running

class Simulation( object ):
    """A built Containernet network together with its graph."""

    def __init__( self, graph: SimGraph, net, nodes: dict,
                  links: dict ) -> None:
        """graph: completed SimGraph
           net: Containernet instance
           nodes: node name -> Mininet node
           links: link id -> Mininet link"""
        self.graph = graph
        self.net = net
        self.nodes = nodes
        self.links = links
        self.started = False

    def node( self, name: str ):
        """Return the Mininet node with this name."""
        return self.nodes[ name ]

    def link( self, linkId: str ):
        """Return the Mininet link with this id."""
        return self.links[ linkId ]

    def start( self ) -> Simulation:
        """Start the network, install routes, apply link states and run
           the configured commands."""
        if self.started:
            return self
        self.net.start()
        self._installRoutes()
        self._applyLinkStates()
        self._runCommands()
        self.started = True
        return self

    def stop( self ) -> None:
        """Stop the network and remove its containers."""
        self.net.stop()
        self.started = False

    def cli( self ) -> None:
        """Open the interactive Mininet CLI."""
        from mininet.cli import CLI
        CLI( self.net )

    def __enter__( self ) -> Simulation:
        """Start on entering a with-block."""
        return self.start()

    def __exit__( self, *_exc ) -> None:
        """Stop on leaving a with-block."""
        self.stop()

    def _installRoutes( self ) -> None:
        """Install configured and generated routes with iproute2."""
        for name, spec in self.graph.nodes.items():
            if not spec.routes:
                continue
            node = self.nodes[ name ]
            if any( route.destination == DEFAULT_ROUTE
                    for route in spec.routes ):
                # replaces Docker's default route via its bridge network
                node.netCmd( 'ip', 'route', 'del', 'default' )
            for route in spec.routes:
                result = node.netCmd( *route.argv() )
                if result.strip():
                    warn( '*** simconfig %s: %s failed: %s' % (
                        name, ' '.join( route.argv() ), result ) )

    def _applyLinkStates( self ) -> None:
        """Bring links configured as 'down' down (after routing)."""
        for link in self.graph.links:
            if link.state == 'down':
                mnLink = self.links[ link.id ]
                mnLink.intf1.set_state( 'down' )
                mnLink.intf2.set_state( 'down' )

    def _runCommands( self ) -> None:
        """Run 'exec' commands and image entrypoints ('start')."""
        for name, spec in self.graph.nodes.items():
            node = self.nodes[ name ]
            for command in spec.commands:
                node.cmd( command )
            if spec.start:
                node.start()


class SimulationBuilder( object ):
    """Creates Containernet objects from a completed SimGraph."""

    # configuration key -> Docker keyword argument
    DOCKER_ARGS = { 'cmd': 'dcmd', 'env': 'environment', 'shell': 'dshell' }
    SWITCH_ARGS = { 'fail_mode': 'failMode' }

    def __init__( self, graph: SimGraph ) -> None:
        """graph: completed graph (see GraphCompleter)"""
        self.graph = graph

    def build( self ) -> Simulation:
        """Create the network (containers start; links are wired) without
           starting it. Requires root."""
        # Imported here so parsing works without root or Open vSwitch
        from mininet.link import TCIntf, TCLink
        from mininet.net import Containernet
        from mininet.node import OVSBridge, OVSSwitch, RemoteController

        net = Containernet( controller=None, intf=TCIntf, link=TCLink )
        for controller in self.graph.controllers:
            net.addController( controller[ 'name' ],
                               controller=RemoteController,
                               ip=controller[ 'ip' ],
                               port=controller[ 'port' ] )
        switchCls = OVSSwitch if self.graph.controllers else OVSBridge
        nodes = {}
        try:
            for name, spec in self.graph.nodes.items():
                if spec.kind is NodeKind.SWITCH:
                    nodes[ name ] = net.addSwitch(
                        name, cls=switchCls, **self.switchArgs( spec ) )
                else:
                    kwargs = { 'dimage': spec.image, 'ip': None, 'rm': True }
                    kwargs.update( self.dockerArgs( spec ) )
                    nodes[ name ] = net.addDocker( name, **kwargs )
            links = {}
            for link in self.graph.links:
                links[ link.id ] = net.addLink(
                    nodes[ link.a.node ], nodes[ link.b.node ], cls=TCLink,
                    intfName1=link.a.intf, intfName2=link.b.intf,
                    params1=self._intfParams( link.a ),
                    params2=self._intfParams( link.b ), **link.tc )
        except BaseException:
            net.stop()
            raise
        return Simulation( self.graph, net, nodes, links )

    @staticmethod
    def _intfParams( endpoint: Endpoint ) -> dict:
        """Interface parameters for one side of a link."""
        return { 'ip': str( endpoint.ip ) } if endpoint.ip is not None else {}

    def dockerArgs( self, spec: NodeSpec ) -> dict:
        """Translate configuration options into Docker keyword arguments."""
        options = dict( spec.options )
        args = dict( options.pop( 'params', None ) or {} )
        for key in ConfigParser.SWITCH_KEYS:
            options.pop( key, None )
        ports = options.pop( 'ports', None ) or []
        if ports:
            args[ 'ports' ], args[ 'port_bindings' ] = self.parsePorts( ports )
        if 'sysctls' in options:
            options[ 'sysctls' ] = { key: str( value ) for key, value
                                     in options[ 'sysctls' ].items() }
        for key, value in options.items():
            args[ self.DOCKER_ARGS.get( key, key ) ] = value
        return args

    def switchArgs( self, spec: NodeSpec ) -> dict:
        """Translate configuration options into addSwitch keyword args."""
        options = dict( spec.options )
        args = dict( options.pop( 'params', None ) or {} )
        if self.graph.controllers:
            args.setdefault( 'protocols', 'OpenFlow13' )
        for key in ConfigParser.SWITCH_KEYS:
            if key in options:
                args[ self.SWITCH_ARGS.get( key, key ) ] = str( options[ key ] )
        return args

    @staticmethod
    def parsePorts( ports: Iterable[ Any ] ) -> tuple:
        """Parse 'host:container' / container port entries.
           returns: ( container ports, { container: host or None } )"""
        containerPorts, bindings = [], {}
        for entry in ports:
            text = str( entry )
            host, _, container = text.rpartition( ':' )
            containerPort = int( container )
            containerPorts.append( containerPort )
            bindings[ containerPort ] = int( host ) if host else None
        return containerPorts, bindings


# Convenience API

def loadGraph( source: Union[ str, Path, dict ],
               log: Optional[ InferenceLog ] = None ) -> SimGraph:
    """Parse and complete a configuration (no root needed).
       source: path to a YAML/JSON file, or an already loaded mapping
       log: inference log to use
       returns: completed SimGraph"""
    log = log or InferenceLog()
    parser = ConfigParser( log )
    data = source if isinstance( source, dict ) else parser.readFile( source )
    return GraphCompleter().complete( parser.parse( data ) )


def loadSimulation( source: Union[ str, Path, dict ],
                    log: Optional[ InferenceLog ] = None ) -> Simulation:
    """Parse, complete and build a simulation (requires root).
       Use start()/stop() or a with-block to run it."""
    return SimulationBuilder( loadGraph( source, log ) ).build()


def main( argv: Optional[ list ] = None ) -> int:
    """Command line entry point; see the module documentation."""
    parser = argparse.ArgumentParser(
        prog='python3 -m mininet.simconfig',
        description='Build a Containernet simulation from a YAML/JSON file.' )
    parser.add_argument( 'config', help='simulation configuration file' )
    parser.add_argument( '--dry-run', action='store_true',
                         help='complete the graph and print it; build nothing' )
    parser.add_argument( '--resolved', metavar='FILE',
                         help='write the completed configuration to FILE' )
    parser.add_argument( '--no-cli', action='store_true',
                         help='start and stop without opening the CLI' )
    parser.add_argument( '--log-level', default='info',
                         help='mininet log level (default: info)' )
    args = parser.parse_args( argv )
    setLogLevel( args.log_level )
    try:
        graph = loadGraph( args.config )
    except ( SimConfigError, OSError ) as exc:
        sys.stderr.write( 'simconfig: %s\n' % exc )
        return 2
    if args.resolved:
        Path( args.resolved ).write_text( graph.dumps() )
    if args.dry_run:
        if not args.resolved:
            sys.stdout.write( graph.dumps() )
        return 0
    simulation = SimulationBuilder( graph ).build()
    try:
        simulation.start()
        if not args.no_cli:
            simulation.cli()
    finally:
        simulation.stop()
    return 0


if __name__ == '__main__':
    sys.exit( main() )
