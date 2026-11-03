"""Build Containernet simulations from a YAML topology file.

This module turns a description of a network into a running emulated
network. A simulation is described as a graph of ``nodes`` (hosts, routers
and switches) and ``links`` between them, in YAML (JSON works too, since
JSON is valid YAML). Every field except the links themselves is optional:
whatever is left out is inferred, and every inference is logged so the
result is never a surprise. The fully resolved graph can be written back
out (``--resolved``) to pin it down or edit it.

How the module is organized
---------------------------
Loading a topology happens in three stages, and the sections of this file
follow them in order.

1. Parsing. ``ConfigParser`` reads the file and checks every value. It
   produces a raw ``SimGraph``: ``NodeSpec`` and ``LinkSpec`` dataclasses
   (holding ``Endpoint``, ``Shaping``, ``NodeOptions`` and ``RouteSpec``
   values) that still have blanks wherever the file left something out.
   Unknown keys produce warnings with a "did you mean" suggestion; invalid
   values raise ``SimConfigError``.

2. Completion. ``GraphCompleter`` runs a fixed sequence of completion
   passes (subclasses of ``CompletionPass``). Each pass fills in one kind
   of blank and may rely on the passes before it:

   - ``NodeDiscovery`` creates the nodes that are only named in links.
   - ``KindInference`` decides whether each node is a host, a router or a
     switch.
   - ``NodeDefaults`` applies the per-kind defaults (image, Docker
     settings, the sysctls that make a router forward packets).
   - ``SwitchIdentity`` gives every switch a unique datapath ID.
   - ``InterfaceNaming`` names every interface ``<node>-eth<N>``.
   - ``LinkIdentity`` gives every link a unique id.
   - ``SegmentDiscovery`` groups the interfaces into layer-2 segments,
     each of which becomes one IP subnet.
   - ``AddressPlanning`` picks a subnet for each segment and an address
     for each interface.
   - ``RoutePlanning`` generates static routes: shortest paths, plus
     loop-free backup routes over redundant links.

   Everything a pass fills in is recorded in an ``InferenceLog``.

3. Building. ``SimulationBuilder`` turns the completed graph into
   Containernet objects (Docker containers, Open vSwitch switches and
   shaped links) and returns a ``Simulation``, which starts the network,
   installs the routes, applies the link states and runs the start-up
   commands.

``loadGraph`` (stages 1 and 2, no root needed) and ``loadSimulation`` (all
three stages, needs root) are the convenience entry points, and ``main``
is the command line. In this project, ``network_builder/config_loader.py``
runs stages 1 and 2 with ``ConfigParser`` and ``GraphCompleter`` and then
builds the network through its own topology class instead of stage 3.

Terms used in this module
-------------------------
- Host: an end device (a Docker container) that sends and receives
  traffic but does not forward it for others.
- Router: a Docker container with IP forwarding enabled; it passes packets
  between its interfaces according to its routing table.
- Switch: an Open vSwitch (OVS) bridge, a software Ethernet switch. It has
  no IP addresses of its own; it joins everything plugged into it into one
  LAN.
- L2 and L3: layer 2 (Ethernet, which delivers frames within one LAN) and
  layer 3 (IP, which routes packets between subnets). Hosts and routers
  are L3 nodes because they own IP addresses; switches work at L2 only.
- Segment: one layer-2 broadcast domain, either a direct link between two
  L3 nodes or everything attached to a group of interconnected switches.
  Each segment is one IP subnet.
- Point-to-point (p2p) link: a direct link between two routers. It gets a
  small /30 subnet, which has exactly two usable addresses.
- Static route: a fixed routing table entry, "to reach network X, send to
  neighbour Y through interface Z", installed with ``ip route``. Its
  metric ranks routes to the same destination: the kernel uses the
  available route with the lowest metric.
- Shaping: traffic control with Linux tc, here through Mininet's TCIntf.
  netem (the kernel's network emulator) adds delay, jitter and loss, and
  HTB or TBF queues (token bucket schedulers) limit the bandwidth.

Fidelity levels
---------------
The same network can be described with as much or as little detail as
needed. The three examples below describe one network: host d1 behind
edge router er1, host d2 behind edge router er2, and a WAN router between
the edge routers. er1 has two parallel links to the WAN router, so one can
serve as a backup for the other.

Low: just the shape of the network. Node kinds are guessed from names,
images and the number of links; subnets, addresses, interface names and
static routes (with loop-free backup routes over redundant links) are
generated::

    links:
      - d1 -- er1
      - er1 -- wan
      - er1 -- wan          # parallel link, so a backup route is added
      - wan -- er2
      - er2 -- d2

Medium: roles, images and a few constraints; the rest is filled in. er1
and er2 are not declared as nodes, so they are created from the links
(and become routers because their names contain 'er')::

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

High: everything explicit; only what is still missing (for example
wan_r's routes) is generated. This example is shortened to the d1 side of
the network::

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
The top-level keys are ``name``, ``defaults``, ``addressing``,
``routing``, ``controllers``, ``nodes`` and ``links``.

``name``
    The simulation's name (default "simulation").

``defaults``
    Settings that every node of a kind starts from, under ``host``,
    ``router`` and ``switch`` (written like a node's settings), and
    settings that every link starts from, under ``link`` (cost, state and
    shaping). A node's or link's own settings are layered over them:
    mappings such as ``sysctls`` merge key by key, and any other value set
    on the node or link replaces the default. Of a node default, only
    ``image``, ``start``, ``exec``, ``params`` and the Docker and switch
    settings are used.

``nodes``
    A mapping from each node name to the node's settings, to just a kind
    name, or to nothing; or a list of node names and of mappings with a
    ``name`` key. Nodes that only appear in links are created
    automatically. A name starts with a letter or digit and contains only
    letters, digits, "_", "." and "-". The settings are:

    - ``kind``: host, router or switch (inferred if omitted; aliases such
      as server, gw and bridge are accepted).
    - ``image``: the Docker image (hosts and routers).
    - ``ip``: an address for the node's first interface that has none
      (with /24 if no prefix is given).
    - ``gateway``: the default gateway (hosts).
    - ``routes``: static routes, as "DEST [via IP] [dev INTF] [metric N]"
      strings (the parts in this order) or as mappings with ``to``,
      ``via``, ``dev`` and ``metric``. DEST may be "default".
    - ``exec``: a command, or a list of commands, run in the node after
      the network is up.
    - ``start``: true to run the image's ENTRYPOINT and CMD after the
      network is up (Containernet does not run them when it creates the
      container, so that they start with the network in place).
    - Docker options (see mininet.node.Docker): ``cmd``, ``env`` (a
      mapping or a list of KEY=value; ``environment`` is the same
      setting), ``ports`` ("8080:80" publishes container port 80 on host
      port 8080, a bare 80 publishes it on a free host port),
      ``volumes``, ``privileged``, ``sysctls``, ``cpus``, ``mem_limit``,
      ``memswap_limit``, ``cpu_shares``, ``cpu_quota``, ``cpu_period``,
      ``cpuset_cpus``, ``dns``, ``devices``, ``cap_add`` and ``shell``
      (bash or sh).
    - Switch options: ``dpid`` (the OpenFlow datapath ID, in hexadecimal),
      ``protocols`` (for example OpenFlow13) and ``fail_mode`` (secure or
      standalone).
    - ``params``: raw keyword arguments passed to addDocker() or
      addSwitch().

``links``
    A list of links. Each is an "a -- b" string ("a -> b" and "a <-> b"
    are accepted too; every link carries traffic both ways), an [a, b]
    pair, or a mapping with ``a`` and ``b`` (or ``endpoints: [a, b]``). An
    endpoint is a node name or a mapping with ``node``, ``intf`` and
    ``ip``. The link settings are:

    - ``id``: the link's name (default "<intfA>-<intfB>").
    - ``cost``: the routing weight, a positive number (default 1). Routes
      follow the path with the lowest total cost.
    - ``state``: up or down. A down link exists but is switched off once
      the network has started.
    - Shaping: ``delay`` and ``jitter`` (durations such as 20ms, 250us or
      1.5s; a bare number means milliseconds), ``loss`` (percent), ``bw``
      (Mbit/s), ``max_queue_size`` (packets), and ``tc``, a mapping with
      the other TCIntf.config() options (use_hfsc, use_tbf, latency_ms,
      enable_ecn, enable_red, speedup, gro, txo and rxo). Each end of a
      link shapes the traffic it sends, so a link's delay applies in each
      direction and adds twice to a round trip. An endpoint may set the
      shaping keys too: they shape the traffic that end sends, layered
      over the link's own.

``addressing``
    ``p2p_pool`` and ``p2p_prefix`` give the subnets of router-to-router
    links (default 10.255.0.0/16 split into /30s); ``lan_pool`` and
    ``lan_prefix`` give the subnets of everything else (default
    192.168.0.0/16 split into /24s, enlarged for a LAN with more
    interfaces than fit). Each prefix must lie between its pool's prefix
    and /30. ``addressing: false`` (or ``enabled: false`` inside the
    mapping) disables automatic addressing.

``routing``
    ``mode`` is static (the default: routes along the shortest paths by
    link cost, passing only through routers) or none (only the configured
    routes are installed). ``backup_routes`` (default true) adds loop-free
    alternates (RFC 5286) with higher metrics: when a router's interface
    on the primary link goes down, the kernel drops the routes through it
    and the backup takes over. ``routing`` may also be just the mode.

``controllers``
    OpenFlow controllers that run elsewhere: a list of names, or of
    mappings with ``name``, ``ip`` (default 127.0.0.1) and ``port``
    (default 6653). When controllers exist, switches become OpenFlow 1.3
    switches managed by them; otherwise they are standalone learning
    bridges.

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
from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Iterable, Iterator, Mapping

from mininet.log import info, warn, setLogLevel

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a containernet dependency
    # Without pyyaml, files are read and written as JSON
    yaml = None

# Short names for the ipaddress classes used throughout. An IPv4Interface is
# an address together with its subnet (for example 10.0.1.2/30), as an
# interface is configured; an IPv4Network is a subnet (10.0.1.0/30); an
# IPv4Address is a bare address (10.0.1.1), as used for a route's next hop.
IPv4Interface = ipaddress.IPv4Interface
IPv4Network = ipaddress.IPv4Network
IPv4Address = ipaddress.IPv4Address

#: Linux limits interface names to IFNAMSIZ - 1 characters (IFNAMSIZ is 16,
#: one byte is the terminating NUL)
MAX_INTF_NAME_LEN = 15
#: The destination of a default route: every address (written "default" in
#: route strings and in ``ip route``)
DEFAULT_ROUTE = IPv4Network( '0.0.0.0/0' )


class SimConfigError( Exception ):
    """Raised for configurations that cannot be parsed or completed.

       The message names the offending part of the file (for example
       "links[3].a" or "node er1")."""


class NodeKind( str, Enum ):
    """Role of a node in the simulated network.

       Deriving from str lets a kind compare equal to its name ('host')
       and be written to YAML as a plain string through ``value``."""

    #: An end device (Docker container) that does not forward traffic
    HOST = 'host'
    #: A Docker container that forwards IP packets between its interfaces
    ROUTER = 'router'
    #: An Open vSwitch bridge (layer 2 only, no IP addresses)
    SWITCH = 'switch'

    @classmethod
    def parse( cls, value: str ) -> NodeKind:
        """Parse a kind name, accepting a few common aliases.

           :param value: a kind name such as 'host', 'router' or 'switch',
               or an alias such as 'server', 'gw' or 'bridge'; case and
               surrounding spaces are ignored
           :returns: the matching kind
           :raises SimConfigError: for unknown kinds"""
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
        """Whether nodes of this kind own IP addresses (layer 3 nodes).

           :returns: True for hosts and routers, False for switches"""
        return self is not NodeKind.SWITCH


# The other fixed sets of values a file may contain. Like NodeKind they
# derive from str, so each member compares equal to its value ('up') and
# is written to YAML through ``value``.

class LinkState( str, Enum ):
    """Whether a link is on once the network has started."""

    #: Working normally (the default)
    UP = 'up'
    #: Created but switched off: both its interfaces are set down after the
    #: network starts, as if the cable were unplugged
    DOWN = 'down'


class FailMode( str, Enum ):
    """What an Open vSwitch switch does while it has no controller."""

    #: Forward nothing until the controller installs rules
    SECURE = 'secure'
    #: Act as an ordinary learning switch
    STANDALONE = 'standalone'


class RoutingMode( str, Enum ):
    """The 'routing' section's mode: whether routes are generated."""

    #: Generate the missing static routes from the topology (the default)
    STATIC = 'static'
    #: Install only the routes the file lists
    NONE = 'none'


class RouteOrigin( str, Enum ):
    """Where a route came from."""

    #: Listed in the file
    CONFIG = 'config'
    #: Generated by RoutePlanning
    AUTO = 'auto'


class Severity( str, Enum ):
    """How important an inference Note is."""

    #: A value that was filled in
    ASSUMED = 'assumed'
    #: Something that looks wrong but does not stop the build
    WARNING = 'warning'


# Inference log
#
# Completing a graph fills in many values the file did not give. Each one is
# recorded as a Note, so a user can see (and, through --resolved, pin down)
# exactly what was assumed.

@dataclass( frozen=True )
class Note( object ):
    """One inference or warning produced while completing the graph."""

    #: Whether this is a filled-in value or a warning
    severity: Severity
    #: What the note is about: a node, interface, link or section name
    subject: str
    #: What was assumed and why, or what looks wrong
    message: str

    def __str__( self ) -> str:
        """Human readable form.

           :returns: for example '[assumed] er1: kind router (2 links)'"""
        return '[%s] %s: %s' % ( self.severity.value, self.subject,
                                 self.message )


class InferenceLog( object ):
    """Collects everything the loader assumed or found suspicious.

       Notes are kept in ``notes`` and, unless disabled, also printed
       through mininet.log as they are recorded."""

    def __init__( self, echo: bool = True ) -> None:
        """Create an empty log.

           :param echo: also print each note through mininet.log (info for
               assumptions, warn for warnings)"""
        #: Every note recorded so far, in order
        self.notes: list[ Note ] = []
        self.echo = echo

    def assume( self, subject: str, message: str ) -> None:
        """Record a value that was filled in.

           :param subject: node, interface, link or section the note is
               about
           :param message: what was assumed and why"""
        self._add( Note( Severity.ASSUMED, subject, message ), info )

    def warning( self, subject: str, message: str ) -> None:
        """Record something that looks wrong but does not stop the build.

           :param subject: node, interface, link or section the note is
               about
           :param message: description of the problem"""
        self._add( Note( Severity.WARNING, subject, message ), warn )

    def _add( self, note: Note, emit ) -> None:
        """Store a note and optionally print it.

           :param note: the note to record
           :param emit: mininet.log function to print it with (info or
               warn)"""
        self.notes.append( note )
        if self.echo:
            emit( '*** simconfig %s\n' % note )

    def report( self ) -> str:
        """Return all notes as text.

           :returns: the notes, one per line"""
        return '\n'.join( str( note ) for note in self.notes )


# Configuration values
#
# Dataclasses for the settings of the simulation's objects (shaping, Docker
# and switch options, port bindings, controllers). A field left None is not
# set, so layers combine field by field with merged(): first the built-in
# defaults, then the file's 'defaults' section, then the node or link
# itself. Mappings (such as sysctls) merge key by key; any other value set
# in a later layer replaces the earlier one.

def _merged( base: Any, override: Any ) -> Any:
    """Combine two layers of one setting (see the section comment).

       :param base: the earlier layer's value (None if not set)
       :param override: the later layer's value (None if not set)
       :returns: a deep copy of override, of base if override is None, or
           of both merged if both are mappings"""
    if override is None:
        return copy.deepcopy( base )
    if isinstance( base, dict ) and isinstance( override, dict ):
        return _deepMerge( base, override )
    return copy.deepcopy( override )


def _mergedFields( base: Any, override: Any ) -> Any:
    """Layer one settings dataclass over another, field by field.

       :param base: the earlier layer (a dataclass instance)
       :param override: the later layer, of the same class
       :returns: a new instance with override's settings layered over
           base's (see _merged); neither input is modified"""
    return replace( base, **{ f.name: _merged( getattr( base, f.name ),
                                               getattr( override, f.name ) )
                              for f in fields( base ) } )


#: A duration: a whole or decimal number, optional spaces, then an optional
#: unit (us for microseconds, ms for milliseconds, s for seconds); groups
#: are the number and the unit
_DURATION = re.compile( r'^(\d+(?:\.\d+)?)\s*(us|ms|s)?$' )
#: Milliseconds per unit of a duration
_TIME_MS = { 'us': 0.001, 'ms': 1.0, 's': 1000.0 }


def _duration( value: Any, where: str ) -> str:
    """Parse a duration as tc understands it.

       :param value: for example '20ms', '250us', '1.5s', '40 ms' or a bare
           number, which means milliseconds ('40' is '40ms')
       :param where: position in the file, for error messages
       :returns: the duration in tc's form, for example '40ms'
       :raises SimConfigError: if value is not a duration"""
    match = _DURATION.match( str( value ).strip() )
    if not match or isinstance( value, bool ):
        raise SimConfigError( '%s: %r is not a duration (e.g. 20ms, 250us, '
                              '1.5s or 40 for milliseconds)' % ( where, value ) )
    return '%s%s' % ( match.group( 1 ), match.group( 2 ) or 'ms' )


def _number( value: Any, where: str, kind: type = float ) -> Any:
    """Parse a number.

       Strings are accepted, and a trailing '%' is dropped so that '2%' is
       2 (for percentages). Booleans are rejected even though Python treats
       them as numbers.

       :param value: the configured value
       :param where: position in the file, for error messages
       :param kind: int to require a whole number, float (the default) to
           accept any number
       :returns: an int if the number is whole, otherwise a float (so
           written-out configurations show 1 rather than 1.0)
       :raises SimConfigError: if value is not a number of that kind"""
    try:
        if isinstance( value, bool ):
            raise ValueError
        number = float( str( value ).strip().rstrip( '%' ) )
        if kind is int:
            if not number.is_integer():
                raise ValueError
            return int( number )
        return int( number ) if number.is_integer() else number
    except ValueError:
        raise SimConfigError( '%s: %r is not %s' % (
            where, value, 'a whole number' if kind is int else 'a number' ) )


def _flag( value: Any, where: str ) -> bool:
    """Check a true or false setting.

       :param value: the configured value; it must be a YAML boolean, not a
           string or number
       :param where: position in the file, for error messages
       :returns: value
       :raises SimConfigError: if value is not a boolean"""
    if not isinstance( value, bool ):
        raise SimConfigError( '%s: %r is not true or false' % ( where, value ) )
    return value


@dataclass
class Shaping( object ):
    """Traffic control (tc) settings of one interface.

       The fields are the options of TCIntf.config() in mininet.link, which
       sets up netem (the Linux kernel's network emulator, which adds
       delay, jitter and loss) and a bandwidth-limiting queue (HTB by
       default, or HFSC or TBF). Shaping applies to the traffic an
       interface sends. None means not set, so TCIntf's default applies."""

    #: The options a link or endpoint may set directly; the others go in 'tc'
    BASIC: ClassVar[ tuple ] = ( 'delay', 'jitter', 'loss', 'bw',
                                 'max_queue_size' )

    #: Added delay in tc's form, for example '20ms'
    delay: str | None = None
    #: Random variation of the delay (plus or minus), same form; needs a
    #: delay
    jitter: str | None = None
    #: Share of packets dropped at random, in percent (0 to 100)
    loss: float | None = None
    #: Bandwidth limit in Mbit/s
    bw: float | None = None
    #: Length limit of netem's queue, in packets
    max_queue_size: int | None = None
    #: Use the HFSC scheduler instead of HTB for the bandwidth limit
    use_hfsc: bool | None = None
    #: Use a TBF (token bucket filter) instead of HTB for the bandwidth limit
    use_tbf: bool | None = None
    #: TBF's latency parameter (how long a packet may wait), in milliseconds
    latency_ms: float | None = None
    #: Mark congestion with ECN (explicit congestion notification) instead
    #: of dropping
    enable_ecn: bool | None = None
    #: Use a RED (random early detection) queue, which drops packets before
    #: the queue is full
    enable_red: bool | None = None
    #: Experimental switch-side bandwidth option of TCIntf
    speedup: float | None = None
    #: Generic receive offload (merging received packets into larger ones
    #: before they are processed)
    gro: bool | None = None
    #: Transmit checksum offload
    txo: bool | None = None
    #: Receive checksum offload
    rxo: bool | None = None

    @classmethod
    def fromDict( cls, data: Mapping[ str, Any ], where: str ) -> Shaping:
        """Parse shaping options in configuration form.

           :param data: option name to value; durations may be bare
               numbers (milliseconds) and numbers may be strings
           :param where: position in the file, for error messages
           :returns: the parsed shaping
           :raises SimConfigError: for unknown options and invalid values"""
        known = { f.name for f in fields( cls ) }
        unknown = sorted( set( data ) - known )
        if unknown:
            raise SimConfigError( '%s: unknown shaping option(s) %s' % (
                where, ', '.join( map( str, unknown ) ) ) )
        values: dict = {}
        for key, value in data.items():
            at = '%s.%s' % ( where, key )
            if value is None:
                continue
            if key in ( 'delay', 'jitter' ):
                values[ key ] = _duration( value, at )
            elif key in ( 'loss', 'bw', 'latency_ms', 'speedup' ):
                values[ key ] = _number( value, at )
            elif key == 'max_queue_size':
                values[ key ] = _number( value, at, int )
            else:
                values[ key ] = _flag( value, at )
        if values.get( 'loss' ) is not None and not 0 <= values[ 'loss' ] <= 100:
            raise SimConfigError( '%s.loss: %s%% is not a percentage'
                                  % ( where, values[ 'loss' ] ) )
        return cls( **values )

    def merged( self, override: Shaping ) -> Shaping:
        """Layer another shaping over this one.

           :param override: the options that take precedence
           :returns: a new Shaping with override's options layered over
               this one's"""
        return _mergedFields( self, override )

    def params( self ) -> dict:
        """The options that are set, as a flat mapping.

           :returns: option name to value, usable both as keyword arguments
               for TCIntf.config() and in configuration form"""
        return { f.name: getattr( self, f.name ) for f in fields( self )
                 if getattr( self, f.name ) is not None }

    def toDict( self ) -> dict:
        """Configuration form, as a link or endpoint is written.

           :returns: the BASIC options as keys of their own and the others
               in a nested 'tc' mapping"""
        result: dict = {}
        for key, value in self.params().items():
            if key in self.BASIC:
                result[ key ] = value
            else:
                result.setdefault( 'tc', {} )[ key ] = value
        return result

    def __bool__( self ) -> bool:
        """Whether any option is set.

           :returns: False for a Shaping with every option None"""
        return bool( self.params() )

    @staticmethod
    def milliseconds( duration: str | None ) -> float | None:
        """Convert a duration to milliseconds.

           :param duration: a duration as stored in a Shaping ('20ms',
               '250us', '1.5s'), or None
           :returns: the duration in milliseconds, or None for None"""
        if duration is None:
            return None
        match = _DURATION.match( duration )
        return float( match.group( 1 ) ) * _TIME_MS[ match.group( 2 ) or 'ms' ]

    def describe( self ) -> str:
        """Short description for log messages.

           :returns: the options that are set, for example
               'delay 20ms, loss 1'"""
        return ', '.join( '%s %s' % item for item in self.params().items() )


@dataclass
class PortBinding( object ):
    """A container port published on the Docker host.

       Traffic sent to the host port reaches the container port, so
       services inside the simulation can be reached from outside it."""

    #: The port inside the container
    containerPort: int
    #: The port on the Docker host; None lets Docker pick a free (ephemeral)
    #: port
    hostPort: int | None = None

    @classmethod
    def parse( cls, value: Any, where: str ) -> PortBinding:
        """Parse a port setting.

           :param value: a container port such as 80, or 'HOST:CONTAINER'
               such as '8080:80'
           :param where: position in the file, for error messages
           :returns: the binding
           :raises SimConfigError: if value is not a port or a port is
               outside 1 to 65535"""
        text = str( value ).strip()
        # Split at the last colon; without one, host is '' (no host port)
        host, _, container = text.rpartition( ':' )
        try:
            binding = cls( int( container ), int( host ) if host else None )
        except ValueError:
            raise SimConfigError( '%s: %r is not a port (e.g. 80 or '
                                  '"8080:80")' % ( where, value ) )
        for port in ( binding.containerPort, binding.hostPort ):
            if port is not None and not 0 < port < 65536:
                raise SimConfigError( '%s: port %d is out of range'
                                      % ( where, port ) )
        return binding

    def toConfig( self ) -> int | str:
        """Configuration form.

           :returns: the container port (80), or 'HOST:CONTAINER'
               ('8080:80') for a fixed host port"""
        if self.hostPort is None:
            return self.containerPort
        return '%d:%d' % ( self.hostPort, self.containerPort )


def _environment( value: Any, where: str ) -> dict[ str, str ]:
    """Parse environment variables.

       :param value: a mapping, a list of 'KEY=value' strings (an item
           without '=' sets KEY to an empty string), or nothing
       :param where: position in the file, for error messages
       :returns: variable name to value, all strings
       :raises SimConfigError: for any other kind of value"""
    if not value:
        return {}
    if isinstance( value, dict ):
        return { str( k ): str( v ) for k, v in value.items() }
    if isinstance( value, list ):
        # Appending '' gives 'KEY' (no '=') the pair [ 'KEY', '' ]
        return dict( ( str( item ).split( '=', 1 ) + [ '' ] )[ :2 ]
                     for item in value )
    raise SimConfigError( '%s: expected a mapping or a list of KEY=value'
                          % where )


def _strings( value: Any, where: str ) -> list[ str ]:
    """Parse a list of strings.

       :param value: a list, or a single string meaning a list of one
       :param where: position in the file, for error messages
       :returns: the items as strings
       :raises SimConfigError: if value is neither a list nor a string"""
    if isinstance( value, str ):
        return [ value ]
    if not isinstance( value, list ):
        raise SimConfigError( '%s: expected a list' % where )
    return [ str( item ) for item in value ]


def _mapping( value: Any, where: str ) -> dict:
    """Check a mapping setting.

       :param value: the configured value
       :param where: position in the file, for error messages
       :returns: a deep copy of value, so later changes do not reach the
           parsed file
       :raises SimConfigError: if value is not a mapping"""
    if not isinstance( value, dict ):
        raise SimConfigError( '%s: expected a mapping' % where )
    return copy.deepcopy( value )


@dataclass
class NodeOptions( object ):
    """A node's Docker and switch settings.

       The fields use the configuration file's vocabulary (see the module
       documentation); SimulationBuilder translates them into the keyword
       arguments of addDocker() and addSwitch(). None means not set."""

    #: The configuration keys of container settings (both spellings of the
    #: environment setting are listed)
    DOCKER: ClassVar[ tuple ] = (
        'cmd', 'env', 'environment', 'ports', 'volumes', 'privileged',
        'sysctls', 'cpus', 'mem_limit', 'memswap_limit', 'cpu_shares',
        'cpu_quota', 'cpu_period', 'cpuset_cpus', 'dns', 'devices', 'cap_add',
        'shell' )
    #: The configuration keys of switch settings
    SWITCH: ClassVar[ tuple ] = ( 'dpid', 'protocols', 'fail_mode' )

    #: The container's command (Docker CMD)
    cmd: str | None = None
    #: Environment variables; 'env' and 'environment' are the same setting,
    #: and they are combined
    environment: dict[ str, str ] | None = None
    #: Container ports published on the Docker host
    ports: list[ PortBinding ] | None = None
    #: Docker volume mounts, for example '/host/dir:/container/dir:rw'
    volumes: list[ str ] | None = None
    #: Run the container with all capabilities and device access
    privileged: bool | None = None
    #: Kernel settings of the container's namespaces, for example
    #: net.ipv4.ip_forward: 1
    sysctls: dict[ str, Any ] | None = None
    #: CPU limit, in CPUs (1.5 is one and a half)
    cpus: float | None = None
    #: Memory limit in bytes, or a Docker size string such as '256m'
    mem_limit: str | int | None = None
    #: Memory plus swap limit, in the same form
    memswap_limit: str | int | None = None
    #: Relative CPU weight (Docker's default is 1024)
    cpu_shares: int | None = None
    #: CPU time allowed per cpu_period, in microseconds
    cpu_quota: int | None = None
    #: Length of the CPU scheduling period, in microseconds
    cpu_period: int | None = None
    #: CPUs the container may run on, for example '0-2' or '0,3'
    cpuset_cpus: str | None = None
    #: DNS servers for the container
    dns: list[ str ] | None = None
    #: Host devices made available in the container
    devices: list[ str ] | None = None
    #: Linux capabilities added to the container, for example NET_ADMIN
    cap_add: list[ str ] | None = None
    #: Shell Containernet uses inside the container ('bash' or 'sh')
    shell: str | None = None
    #: dpid: the OpenFlow datapath ID that identifies a switch to its
    #: controller, as hexadecimal digits (completed to 16 by SwitchIdentity)
    dpid: str | None = None
    #: OpenFlow versions the switch speaks, for example 'OpenFlow13'
    protocols: str | None = None
    #: What a switch does without a controller: 'secure' (forward nothing
    #: until the controller installs rules) or 'standalone' (act as a
    #: learning switch)
    fail_mode: FailMode | None = None
    #: Raw keyword arguments for addDocker() or addSwitch()
    params: dict[ str, Any ] | None = None

    @classmethod
    def fromDict( cls, data: Mapping[ str, Any ], where: str ) -> NodeOptions:
        """Parse the Docker and switch settings among a node's settings.

           Keys that are not Docker or switch settings are ignored; the
           caller handles them.

           :param data: the node's settings in configuration form
           :param where: position in the file, for error messages
           :returns: the parsed options
           :raises SimConfigError: for invalid values"""
        options = cls()
        if 'env' in data or 'environment' in data:
            options.environment = {
                **_environment( data.get( 'env' ), where + '.env' ),
                **_environment( data.get( 'environment' ),
                                where + '.environment' ) }
        for key, value in data.items():
            at = '%s.%s' % ( where, key )
            if value is None or key in ( 'env', 'environment' ):
                continue
            if key == 'ports':
                ports = value if isinstance( value, list ) else [ value ]
                options.ports = [ PortBinding.parse( port, at )
                                  for port in ports ]
            elif key in ( 'volumes', 'dns', 'devices', 'cap_add' ):
                setattr( options, key, _strings( value, at ) )
            elif key in ( 'sysctls', 'params' ):
                setattr( options, key, _mapping( value, at ) )
            elif key == 'privileged':
                options.privileged = _flag( value, at )
            elif key == 'cpus':
                options.cpus = _number( value, at )
            elif key in ( 'cpu_shares', 'cpu_quota', 'cpu_period' ):
                setattr( options, key, _number( value, at, int ) )
            elif key == 'fail_mode':
                if value not in list( FailMode ):
                    raise SimConfigError( "%s: must be 'secure' or "
                                          "'standalone'" % at )
                options.fail_mode = FailMode( value )
            elif key in ( 'mem_limit', 'memswap_limit' ):
                # Bytes, or Docker's size strings such as '256m'
                setattr( options, key, value if isinstance( value, int )
                         else str( value ) )
            elif key in ( 'cmd', 'cpuset_cpus', 'shell', 'dpid', 'protocols' ):
                setattr( options, key, str( value ) )
        return options

    def merged( self, override: NodeOptions ) -> NodeOptions:
        """Layer other settings over these.

           :param override: the settings that take precedence
           :returns: new options with override's settings layered over
               these (mappings merge key by key)"""
        return _mergedFields( self, override )

    def dockerSettings( self ) -> list[ str ]:
        """Names of the container-only settings that are set.

           Used to warn about (and drop) such settings on switches.

           :returns: field names, excluding the switch settings and
               params"""
        docker = [ f.name for f in fields( self ) if f.name not in
                   self.SWITCH + ( 'params', ) ]
        return [ name for name in docker if getattr( self, name ) is not None ]

    def toDict( self ) -> dict:
        """Configuration form.

           :returns: the settings that are set, with the environment under
               'env' and ports as 80 or '8080:80'"""
        result: dict = {}
        for f in fields( self ):
            value = getattr( self, f.name )
            if value is None:
                continue
            if f.name == 'environment':
                result[ 'env' ] = dict( value )
            elif f.name == 'ports':
                result[ 'ports' ] = [ port.toConfig() for port in value ]
            elif isinstance( value, Enum ):
                result[ f.name ] = value.value
            else:
                result[ f.name ] = copy.deepcopy( value )
        return result


@dataclass
class ControllerSpec( object ):
    """An OpenFlow controller the simulation's switches connect to.

       OpenFlow is the protocol through which an SDN (software-defined
       networking) controller tells switches how to forward traffic. This
       base class only names the controller; subclasses say where it
       runs. network_builder/config_loader.py subclasses it for the ONOS
       controllers it starts itself."""

    #: Name of the controller in the Mininet network
    name: str

    def toDict( self ) -> dict:
        """Configuration form.

           :returns: every field, as written in the 'controllers' list"""
        return asdict( self )


@dataclass
class RemoteControllerSpec( ControllerSpec ):
    """A controller that runs elsewhere, reached at ip:port."""

    #: Address the switches connect to
    ip: str = '127.0.0.1'
    #: TCP port of the controller (6653 is the standard OpenFlow port)
    port: int = 6653


# Graph model
#
# The simulation as a graph: NodeSpec for the nodes, LinkSpec (with two
# Endpoints) for the links, and, once SegmentDiscovery has run, Segments
# and the Adjacency between L3 nodes that routing works on.

@dataclass
class Endpoint( object ):
    """One side of a link: the interface a node has on it."""

    #: Name of the node this end is attached to
    node: str
    #: Interface name, for example 'er1-eth0' (filled in by InterfaceNaming)
    intf: str | None = None
    #: Address and prefix length of the interface, for example
    #: 10.0.1.2/30 (filled in by AddressPlanning; always None on a switch)
    ip: IPv4Interface | None = None
    #: False when the configured address had no /prefix (AddressPlanning
    #: then takes the prefix from the rest of the segment)
    explicitPrefix: bool = True
    #: This end's own shaping (the direction it sends), over the link's
    tc: Shaping = field( default_factory=Shaping )

    def toDict( self ) -> dict:
        """Resolved configuration form.

           :returns: node, intf, ip if set, and this end's own shaping"""
        result: dict = { 'node': self.node, 'intf': self.intf }
        if self.ip is not None:
            result[ 'ip' ] = str( self.ip )
        result.update( self.tc.toDict() )
        return result


@dataclass
class LinkSpec( object ):
    """A link between two endpoints.

       In the running network a link is a veth pair (two virtual Ethernet
       interfaces connected back to back), one in each node."""

    #: Position of the link in the file's 'links' list
    index: int
    #: The first end
    a: Endpoint
    #: The second end
    b: Endpoint
    #: Unique name of the link (filled in by LinkIdentity)
    id: str | None = None
    #: Routing weight: routes prefer the path with the lowest total cost
    cost: float = 1.0
    #: 'up', or 'down' for a link switched off once the network has started
    state: LinkState = LinkState.UP
    #: Shaping of both ends; an endpoint's own shaping is layered over it
    tc: Shaping = field( default_factory=Shaping )

    @property
    def endpoints( self ) -> tuple[ Endpoint, Endpoint ]:
        """Both endpoints.

           :returns: ( a, b )"""
        return ( self.a, self.b )

    def shaping( self, side: str ) -> Shaping:
        """The shaping one end of the link is configured with.

           Each end shapes the traffic it sends, so this is the shaping of
           one direction: the link's shaping, with the end's own over it.

           :param side: 'a' or 'b'
           :returns: the combined shaping"""
        return self.tc.merged( self.a.tc if side == 'a' else self.b.tc )

    def label( self ) -> str:
        """Short description for log messages.

           :returns: the id, or (before LinkIdentity has run) the position
               and node names, for example 'link #2 (er1 -- wan)'"""
        return self.id or 'link #%d (%s -- %s)' % ( self.index, self.a.node,
                                                     self.b.node )

    def toDict( self ) -> dict:
        """Resolved configuration form.

           :returns: id, both ends, cost, state and the link's shaping"""
        result = { 'id': self.id, 'a': self.a.toDict(), 'b': self.b.toDict(),
                   'cost': self.cost, 'state': self.state.value }
        result.update( self.tc.toDict() )
        return result


@dataclass
class LinkDefaults( object ):
    """The 'defaults.link' section: settings every link starts from."""

    #: Default routing cost (None: 1)
    cost: float | None = None
    #: Default state (None: 'up')
    state: LinkState | None = None
    #: Shaping that each link's own shaping is layered over
    tc: Shaping = field( default_factory=Shaping )


@dataclass
class RouteSpec( object ):
    """A static IPv4 route: where a node sends packets for a destination."""

    #: The destination network (DEFAULT_ROUTE for the default route)
    destination: IPv4Network
    #: Next hop: the neighbour's address the packets are sent to (None for
    #: a destination reached directly through dev)
    via: IPv4Address | None = None
    #: Interface the packets leave through (None: the kernel picks the one
    #: that reaches via)
    dev: str | None = None
    #: Preference among routes to the same destination: the kernel uses the
    #: available route with the lowest metric
    metric: int | None = None
    #: Whether the route was listed in the file or generated
    origin: RouteOrigin = RouteOrigin.CONFIG

    def argv( self ) -> list[ str ]:
        """The iproute2 command that installs the route.

           :returns: an argument list such as [ 'ip', 'route', 'add',
               '10.0.2.0/24', 'via', '10.255.0.1', 'dev', 'er1-eth1',
               'metric', '1' ]"""
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
        """Resolved configuration form.

           The origin is not written, so a generated route becomes a
           configured one when the resolved file is loaded again.

           :returns: to, and via, dev and metric where set"""
        result: dict = { 'to': ( 'default' if self.destination == DEFAULT_ROUTE
                                 else str( self.destination ) ) }
        for key in ( 'via', 'dev', 'metric' ):
            value = getattr( self, key )
            if value is not None:
                result[ key ] = str( value ) if key == 'via' else value
        return result


@dataclass
class NodeSpec( object ):
    """A node and its settings, in configuration terms.

       Also used as a template for the per-kind defaults (see
       NodeDefaults)."""

    #: Node name, also the Docker container's or switch's name in Mininet
    name: str
    #: Role of the node (None until KindInference has run)
    kind: NodeKind | None = None
    #: Docker image (hosts and routers; None for switches)
    image: str | None = None
    #: Node-level address, given to its first interface without one
    ip: IPv4Interface | None = None
    #: Default gateway (hosts); RoutePlanning fills it in if it can
    gateway: IPv4Address | None = None
    #: Static routes (RouteSpec), configured ones first, then generated
    routes: list = field( default_factory=list )
    #: Commands run in the node once the network is up ('exec')
    commands: list = field( default_factory=list )
    #: Run the image's ENTRYPOINT and CMD once the network is up
    start: bool = False
    #: Docker/switch settings
    options: NodeOptions = field( default_factory=NodeOptions )
    #: False for nodes that only appeared in links
    declared: bool = True

    def toDict( self ) -> dict:
        """Resolved configuration form.

           The node-level ip is not written: by then it is on an
           interface, and written with the link.

           :returns: kind, and the other settings that are set"""
        result: dict = { 'kind': self.kind.value if self.kind else None }
        if self.image:
            result[ 'image' ] = self.image
        if self.gateway is not None:
            result[ 'gateway' ] = str( self.gateway )
        result.update( self.options.toDict() )
        if self.routes:
            result[ 'routes' ] = [ route.toDict() for route in self.routes ]
        if self.commands:
            result[ 'exec' ] = list( self.commands )
        if self.start:
            result[ 'start' ] = True
        return result


@dataclass
class Segment( object ):
    """A layer-2 broadcast domain, which is one IP subnet.

       A broadcast domain is the set of interfaces that reach each other
       directly over Ethernet, without passing a router. A segment is
       either a direct link between two L3 nodes, or everything attached
       to a group of switches that are linked to each other (a LAN)."""

    #: Position in SimGraph.segments
    index: int
    #: Names of the switches that form the LAN (empty for a direct link)
    switches: set = field( default_factory=set )
    #: The L3 interfaces on the segment, as ( LinkSpec, Endpoint ) pairs;
    #: switch ports are not members
    members: list = field( default_factory=list )  # [ ( LinkSpec, Endpoint ) ]
    #: The segment's subnet (filled in by AddressPlanning)
    network: IPv4Network | None = None

    @property
    def isPointToPoint( self ) -> bool:
        """Whether this is a direct link between two L3 nodes.

           :returns: True if there are no switches and two members"""
        return not self.switches and len( self.members ) == 2

    def label( self ) -> str:
        """Short description for log messages.

           :returns: for example 'LAN s1+s2' or 'link er1 -- wan'"""
        if self.switches:
            return 'LAN %s' % '+'.join( sorted( self.switches ) )
        return 'link %s' % ' -- '.join( ep.node for _, ep in self.members )


@dataclass( frozen=True )
class Adjacency( object ):
    """An L3 neighbour of a node: one hop of a route.

       Two L3 nodes are neighbours when they share a segment, so one can
       send packets straight to the other's address. This is the
       neighbourship seen from ``local.node``; the reverse direction is a
       separate Adjacency."""

    #: The segment both interfaces are on
    segment: Segment
    #: The node's own interface on the segment
    local: Endpoint
    #: The neighbour's interface (its address is the next hop of a route)
    peer: Endpoint
    #: Routing cost of the hop (see SimGraph.adjacency)
    cost: float
    #: Creation order, used to break ties between equal costs so that
    #: routes follow the file's declaration order and do not change from
    #: run to run
    order: int


@dataclass
class Settings( object ):
    """Global settings of a simulation."""

    #: Whether AddressPlanning allocates subnets and addresses
    addressing: bool = True
    #: Address pool for point-to-point router-to-router links
    p2pPool: IPv4Network = IPv4Network( '10.255.0.0/16' )
    #: Prefix length of each such link's subnet (/30: two usable addresses)
    p2pPrefix: int = 30
    #: Address pool for every other segment
    lanPool: IPv4Network = IPv4Network( '192.168.0.0/16' )
    #: Prefix length of each LAN subnet (/24: 254 usable addresses)
    lanPrefix: int = 24
    #: Whether missing routes are generated
    routing: RoutingMode = RoutingMode.STATIC
    #: Whether RoutePlanning adds loop-free backup routes
    backupRoutes: bool = True
    #: The 'defaults' section: settings every node of a kind starts from
    nodeDefaults: dict[ NodeKind, NodeSpec ] = field( default_factory=dict )
    #: The 'defaults.link' section
    linkDefaults: LinkDefaults = field( default_factory=LinkDefaults )

    def toDict( self ) -> dict:
        """Resolved configuration form.

           The 'defaults' section is not written: by then it has been
           applied to every node and link.

           :returns: the 'addressing' and 'routing' sections"""
        return {
            'addressing': ( { 'p2p_pool': str( self.p2pPool ),
                              'p2p_prefix': self.p2pPrefix,
                              'lan_pool': str( self.lanPool ),
                              'lan_prefix': self.lanPrefix }
                            if self.addressing else False ),
            'routing': { 'mode': self.routing.value,
                         'backup_routes': self.backupRoutes } }


class SimGraph( object ):
    """The simulation as a graph of nodes, links and L2 segments.

       ConfigParser creates it with blanks; the completion passes fill
       them in place, and SimulationBuilder builds the network from the
       completed graph.

       :ivar name: the simulation's name
       :ivar log: where every inference and warning is recorded
       :ivar settings: global settings (addressing, routing, defaults)
       :ivar nodes: node name to NodeSpec, in declaration order
       :ivar links: the links, in declaration order
       :ivar controllers: the OpenFlow controllers
       :ivar segments: the L2 segments (filled in by SegmentDiscovery)"""

    def __init__( self, name: str = 'simulation',
                  log: InferenceLog | None = None ) -> None:
        """Create an empty graph.

           :param name: simulation name
           :param log: inference log (a new one is created if omitted)"""
        self.name = name
        self.log = log or InferenceLog()
        self.settings = Settings()
        self.nodes: dict[ str, NodeSpec ] = {}
        self.links: list[ LinkSpec ] = []
        self.controllers: list[ ControllerSpec ] = []
        self.segments: list[ Segment ] = []
        # Cache of adjacency(); SegmentDiscovery resets it to None when it
        # rebuilds the segments
        self._adjacency: dict[ str, list[ Adjacency ] ] | None = None

    # Structure

    def addNode( self, spec: NodeSpec ) -> NodeSpec:
        """Add a node.

           :param spec: the node
           :returns: spec
           :raises SimConfigError: for duplicate names"""
        if spec.name in self.nodes:
            raise SimConfigError( 'Duplicate node %r' % spec.name )
        self.nodes[ spec.name ] = spec
        return spec

    def addLink( self, link: LinkSpec ) -> LinkSpec:
        """Add a link.

           :param link: the link
           :returns: link
           :raises SimConfigError: for a link from a node to itself"""
        if link.a.node == link.b.node:
            raise SimConfigError( '%s connects %s to itself'
                                  % ( link.label(), link.a.node ) )
        self.links.append( link )
        return link

    def kindOf( self, name: str ) -> NodeKind | None:
        """Look up the kind of a node.

           :param name: node name
           :returns: the kind (None while still unknown)"""
        return self.nodes[ name ].kind

    def linksOf( self, name: str ) -> list[ LinkSpec ]:
        """Find the links attached to a node.

           :param name: node name
           :returns: the links, in declaration order"""
        return [ link for link in self.links
                 if name in ( link.a.node, link.b.node ) ]

    def endpointsOf( self, name: str ) -> list[ Endpoint ]:
        """Find the endpoints (interfaces) a node owns.

           :param name: node name
           :returns: the endpoints, in declaration order of their links"""
        return [ ep for link in self.links for ep in link.endpoints
                 if ep.node == name ]

    def neighbors( self, name: str ) -> list[ str ]:
        """Find the directly linked nodes.

           :param name: node name
           :returns: the names at the other end of each of its links (with
               repetitions for parallel links)"""
        return [ ( link.b if link.a.node == name else link.a ).node
                 for link in self.linksOf( name ) ]

    def degree( self, name: str ) -> int:
        """Count the links attached to a node.

           :param name: node name
           :returns: the number of links"""
        return len( self.linksOf( name ) )

    # L3 view
    #
    # Routing works on L3 nodes and the segments between them rather than
    # on links: two routers on the same switched LAN are direct neighbours
    # even though no single link joins them.

    def adjacency( self ) -> dict[ str, list[ Adjacency ] ]:
        """The L3 neighbours of every node, through the segments (cached).

           Every pair of members of a segment that belong to different
           nodes are neighbours, in both directions. The cost of a hop is
           the link's cost on a direct link; across a switched LAN it is
           the higher of the two access links' costs (the links from each
           node to the switches).

           Valid once SegmentDiscovery has run.

           :returns: node name to its Adjacency list (empty for switches
               and for nodes without neighbours)"""
        if self._adjacency is None:
            adjacency: dict[ str, list[ Adjacency ] ] = {
                name: [] for name in self.nodes }
            order = 0
            for segment in self.segments:
                for linkX, epX in segment.members:
                    for linkY, epY in segment.members:
                        # A node with two interfaces on one LAN is not its
                        # own neighbour
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
        """Shortest path costs from a set of nodes (Dijkstra's algorithm).

           Dijkstra's algorithm finds the cheapest path from a start node
           to every other node: it repeatedly takes the closest node not
           yet settled (here from a heap, a priority queue) and checks
           whether going through it shortens the path to any neighbour.
           Starting from several sources at once gives each node its
           distance to the nearest source; this is how the distance to a
           segment (from any router on it) is computed.

           Only routers forward traffic, so paths pass only through
           routers: a host is given a distance but its neighbours are not
           reached through it. Hop costs are those of adjacency(), which
           are the same in both directions, so the distance from A to B is
           also the distance from B to A. Links that are configured 'down'
           count like the others.

           :param sources: node name to its starting distance (normally
               0.0)
           :returns: node name to the cost of its shortest path from the
               nearest source; nodes that cannot be reached are missing"""
        dist = dict( sources )
        heap = [ ( d, name ) for name, d in sources.items() ]
        heapq.heapify( heap )
        adjacency = self.adjacency()
        while heap:
            d, name = heapq.heappop( heap )
            # Skip stale heap entries (a shorter distance was found after
            # this one was pushed) and nodes that do not forward
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
        """The completed graph in configuration form.

           The result can be parsed again by ConfigParser and describes
           the same network, with every inferred value written out.

           :returns: the configuration mapping"""
        result: dict = { 'name': self.name }
        result.update( self.settings.toDict() )
        if self.controllers:
            result[ 'controllers' ] = [ controller.toDict()
                                        for controller in self.controllers ]
        result[ 'nodes' ] = { name: spec.toDict()
                              for name, spec in self.nodes.items() }
        result[ 'links' ] = [ link.toDict() for link in self.links ]
        return result

    def dumps( self ) -> str:
        """The completed graph as text.

           :returns: YAML (JSON if pyyaml is missing), keys in the order of
               toDict()"""
        data = self.toDict()
        if yaml is not None:
            return yaml.safe_dump( data, sort_keys=False )
        return json.dumps( data, indent=2 )


# Parsing

def _deepMerge( base: dict, override: dict ) -> dict:
    """Merge two mappings.

       Nested mappings are merged recursively; for any other value, the one
       in override wins. Neither input is modified.

       :param base: the earlier layer
       :param override: the layer that takes precedence
       :returns: a new merged mapping"""
    result = copy.deepcopy( base )
    for key, value in override.items():
        if isinstance( value, dict ) and isinstance( result.get( key ), dict ):
            result[ key ] = _deepMerge( result[ key ], value )
        else:
            result[ key ] = copy.deepcopy( value )
    return result


class ConfigParser( object ):
    """Turns a configuration mapping (or file) into a raw SimGraph.

       Parsing checks the form of every value and converts it (addresses
       to ipaddress objects, durations to tc's form, and so on), but fills
       in nothing that the file leaves out: that is the completion passes'
       job. Unknown keys are reported as warnings, with the closest known
       key as a suggestion."""

    #: The keys allowed at each level of the file
    TOP_KEYS = ( 'name', 'defaults', 'addressing', 'routing', 'controllers',
                 'nodes', 'links' )
    DOCKER_KEYS = NodeOptions.DOCKER
    SWITCH_KEYS = NodeOptions.SWITCH
    NODE_KEYS = ( ( 'name', 'kind', 'image', 'ip', 'gateway', 'routes', 'exec',
                    'start', 'params' ) + DOCKER_KEYS + SWITCH_KEYS )
    TC_KEYS = Shaping.BASIC
    LINK_KEYS = ( ( 'id', 'a', 'b', 'endpoints', 'cost', 'state', 'tc' )
                  + TC_KEYS )
    ENDPOINT_KEYS = ( 'node', 'intf', 'ip' )
    #: The separator of a link string such as 'a -- b': '<->' (or '<-->'
    #: and longer), two or more dashes, or '->', with any spaces around it.
    #: A single dash is not a separator, so node names may contain dashes.
    _linkArrow = re.compile( r'\s*(?:<-+>|-{2,}|->)\s*' )
    #: A route string, 'DEST [via IP] [dev INTF] [metric N]' with the
    #: optional parts in this order; the named groups give the parts
    _routeRegex = re.compile(
        r'^(?P<to>\S+)(?:\s+via\s+(?P<via>\S+))?(?:\s+dev\s+(?P<dev>\S+))?'
        r'(?:\s+metric\s+(?P<metric>\d+))?$' )
    #: A node name: a letter or digit, then letters, digits, '_', '.' or
    #: '-' (names become container and interface names)
    _nameRegex = re.compile( r'^[A-Za-z0-9][A-Za-z0-9_.-]*$' )

    def __init__( self, log: InferenceLog ) -> None:
        """Create a parser.

           :param log: where warnings about the configuration go"""
        self.log = log

    # Entry points

    @staticmethod
    def readFile( path: str | Path ) -> dict:
        """Read a YAML or JSON configuration file.

           :param path: the file
           :returns: its contents
           :raises SimConfigError: if the file is not a mapping
           :raises OSError: if the file cannot be read
           :raises yaml.YAMLError: if the file is not valid YAML (or
               json.JSONDecodeError for invalid JSON without pyyaml)"""
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

           The sections are parsed in an order that lets later ones use
           earlier ones: links start from the link defaults.

           :param data: configuration (see module documentation)
           :returns: the raw graph, still to be completed
           :raises SimConfigError: for invalid configurations"""
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
        """Warn about unknown keys, suggesting close matches (typos).

           Unknown keys are only reported; the callers then ignore them.

           :param mapping: the configured mapping
           :param allowed: the keys it may contain
           :param where: position in the file, for the warning"""
        allowed = list( allowed )
        for key in mapping:
            if key not in allowed:
                hint = difflib.get_close_matches( str( key ), allowed, n=1 )
                self.log.warning( where, 'unknown key %r ignored%s' % (
                    key, ' (did you mean %r?)' % hint[ 0 ] if hint else '' ) )

    @staticmethod
    def _interface( value: Any, where: str ) -> tuple[ IPv4Interface, bool ]:
        """Parse an interface address.

           :param value: an address with or without prefix, for example
               '10.0.1.2/30' or '10.0.1.2'
           :param where: position in the file, for error messages
           :returns: ( IPv4Interface, explicitPrefix ); without a prefix the
               address is a /32 and explicitPrefix is False, so the real
               prefix can be filled in later
           :raises SimConfigError: if value is not an IPv4 address"""
        text = str( value ).strip()
        try:
            return IPv4Interface( text ), '/' in text
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 address %r'
                                  % ( where, value ) )

    @staticmethod
    def _address( value: Any, where: str ) -> IPv4Address:
        """Parse a plain IPv4 address (no prefix).

           :param value: for example '192.168.1.1'
           :param where: position in the file, for error messages
           :returns: the address
           :raises SimConfigError: if value is not an IPv4 address"""
        try:
            return IPv4Address( str( value ).strip() )
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 address %r'
                                  % ( where, value ) )

    @staticmethod
    def _network( value: Any, where: str ) -> IPv4Network:
        """Parse a network.

           :param value: for example '10.0.0.0/8', or 'default' for
               0.0.0.0/0. Host bits are allowed and cleared, so
               '10.0.1.2/24' is 10.0.1.0/24.
           :param where: position in the file, for error messages
           :returns: the network
           :raises SimConfigError: if value is not an IPv4 network"""
        text = str( value ).strip()
        if text == 'default':
            return DEFAULT_ROUTE
        try:
            return IPv4Network( text, strict=False )
        except ValueError:
            raise SimConfigError( '%s: invalid IPv4 network %r'
                                  % ( where, value ) )

    def _parseDefaults( self, graph: SimGraph, data: dict ) -> None:
        """Parse the 'defaults' section into graph.settings.

           Each node kind's defaults are parsed like a node (named, for
           example, 'defaults.host'); the link defaults give the cost,
           state and shaping that every link starts from.

           :param graph: the graph being built
           :param data: the section
           :raises SimConfigError: if a part is not a mapping or is
               invalid"""
        if not isinstance( data, dict ):
            raise SimConfigError( "'defaults' must be a mapping" )
        allowed = [ kind.value for kind in NodeKind ] + [ 'link' ]
        self._checkKeys( data, allowed, 'defaults' )
        for key in allowed:
            section = data.get( key ) or {}
            where = 'defaults.%s' % key
            if not isinstance( section, dict ):
                raise SimConfigError( "%s must be a mapping" % where )
            if key == 'link':
                self._checkKeys( section, self.LINK_KEYS, where )
                cost, state = self._costAndState( section, None, None, where )
                graph.settings.linkDefaults = LinkDefaults(
                    cost=cost, state=state, tc=self._shaping( section, where ) )
            else:
                graph.settings.nodeDefaults[ NodeKind( key ) ] = \
                    self._parseNode( where, section )

    def _parseAddressing( self, graph: SimGraph, data: Any ) -> None:
        """Parse the 'addressing' section into graph.settings.

           :param graph: the graph being built
           :param data: a mapping, or a boolean that only turns automatic
               addressing on or off
           :raises SimConfigError: for invalid pools, or prefixes that do
               not fit in their pool or are longer than /30"""
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
            # A subnet must fit in its pool, and /30 is the smallest subnet
            # that has two usable addresses besides its network and
            # broadcast addresses
            if not pool.prefixlen <= prefix <= 30:
                raise SimConfigError( 'addressing: %s_prefix /%d does not fit '
                                      'in %s' % ( label, prefix, pool ) )

    def _parseRouting( self, graph: SimGraph, data: Any ) -> None:
        """Parse the 'routing' section into graph.settings.

           :param graph: the graph being built
           :param data: a mapping with mode and backup_routes, or just the
               mode
           :raises SimConfigError: for a mode other than static or none"""
        if isinstance( data, str ):
            data = { 'mode': data }
        if not isinstance( data, dict ):
            raise SimConfigError( "'routing' must be a mapping or a mode" )
        self._checkKeys( data, ( 'mode', 'backup_routes' ), 'routing' )
        mode = str( data.get( 'mode', RoutingMode.STATIC.value ) ).lower()
        if mode not in set( RoutingMode ):
            raise SimConfigError( "routing.mode must be 'static' or 'none'" )
        graph.settings.routing = RoutingMode( mode )
        graph.settings.backupRoutes = bool( data.get( 'backup_routes', True ) )

    def _parseControllers( self, data: Any ) -> list[ ControllerSpec ]:
        """Parse the 'controllers' section (controllers running elsewhere).

           :param data: a list of names or of mappings with name, ip and
               port, or a single name or mapping
           :returns: one RemoteControllerSpec per entry; unnamed ones are
               called c0, c1 and so on by position
           :raises SimConfigError: for an invalid port"""
        if isinstance( data, ( str, dict ) ):
            data = [ data ]
        controllers: list[ ControllerSpec ] = []
        for index, item in enumerate( data ):
            where = 'controllers[%d]' % index
            if isinstance( item, str ):
                item = { 'name': item }
            self._checkKeys( item, ( 'name', 'ip', 'port' ), where )
            controllers.append( RemoteControllerSpec(
                name=str( item.get( 'name' ) or 'c%d' % index ),
                ip=str( item.get( 'ip', '127.0.0.1' ) ),
                port=_number( item.get( 'port', 6653 ), where + '.port', int ) ) )
        return controllers

    def _iterNodes( self, data: Any ) -> Iterator[ tuple ]:
        """Go through the 'nodes' section in either of its forms.

           :param data: a mapping from name to settings, or a list of names
               and of mappings with a 'name' key
           :returns: an iterator of ( name, settings ), settings being None
               for a bare name
           :raises SimConfigError: for any other form"""
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
        """Parse one node's settings.

           Also parses each kind's defaults, under names such as
           'defaults.host'.

           :param name: the node name
           :param data: a settings mapping, a kind name, or None
           :returns: the node, with every setting the file leaves out
               unset
           :raises SimConfigError: for an invalid name or settings"""
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
            # Unlike a link endpoint's address, a node-level address gets
            # its prefix here rather than from its segment
            if not explicit:
                spec.ip = IPv4Interface( ( spec.ip.ip, 24 ) )
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
        spec.options = NodeOptions.fromDict(
            { key: value for key, value in data.items()
              if key in self.DOCKER_KEYS + self.SWITCH_KEYS + ( 'params', ) },
            'node %s' % name )
        return spec

    def _parseRoute( self, data: Any, node: str ) -> RouteSpec:
        """Parse one configured route.

           :param data: a string 'DEST [via IP] [dev INTF] [metric N]', or a
               mapping with to (or destination), via, dev and metric
           :param node: the node the route belongs to, for error messages
           :returns: the route
           :raises SimConfigError: for routes that cannot be parsed"""
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
        """Parse one end of a link.

           :param data: a node name, or a mapping with node, intf, ip and
               shaping options
           :param where: position in the file, for error messages
           :returns: the endpoint
           :raises SimConfigError: for an invalid endpoint"""
        if isinstance( data, str ):
            return Endpoint( data.strip() )
        if not isinstance( data, dict ) or not data.get( 'node' ):
            raise SimConfigError( '%s: endpoint must be a node name or a '
                                  'mapping with "node"' % where )
        self._checkKeys( data, self.ENDPOINT_KEYS + self.TC_KEYS + ( 'tc', ),
                         where )
        endpoint = Endpoint( str( data[ 'node' ] ), data.get( 'intf' ),
                             tc=self._shaping( data, where ) )
        if data.get( 'ip' ) is not None:
            endpoint.ip, endpoint.explicitPrefix = self._interface(
                data[ 'ip' ], where )
        return endpoint

    def _shaping( self, data: dict, where: str ) -> Shaping:
        """Parse the shaping of a link or endpoint.

           :param data: the link's or endpoint's mapping
           :param where: position in the file, for error messages
           :returns: the options in its 'tc' mapping, overridden by the
               BASIC options written directly on it
           :raises SimConfigError: for invalid shaping"""
        tc = data.get( 'tc' ) or {}
        if not isinstance( tc, dict ):
            raise SimConfigError( '%s.tc must be a mapping' % where )
        known = [ f.name for f in fields( Shaping ) ]
        self._checkKeys( tc, known, where + '.tc' )
        tc = { key: value for key, value in tc.items() if key in known }
        tc.update( { key: data[ key ] for key in self.TC_KEYS if key in data } )
        return Shaping.fromDict( tc, where )

    @staticmethod
    def _costAndState( data: dict, cost: float | None,
                       state: LinkState | None, where: str
                       ) -> tuple[ float | None, LinkState | None ]:
        """Parse a link's cost and state.

           :param data: the link's (or the link defaults') mapping
           :param cost: the cost to use if data sets none
           :param state: the state to use if data sets none
           :param where: position in the file, for error messages
           :returns: ( cost, state )
           :raises SimConfigError: for a cost that is not a positive number
               or a state other than up or down"""
        if data.get( 'state' ) is not None:
            text = str( data[ 'state' ] ).lower()
            if text not in set( LinkState ):
                raise SimConfigError( "%s: state must be 'up' or 'down'"
                                      % where )
            state = LinkState( text )
        if data.get( 'cost' ) is not None:
            cost = _number( data[ 'cost' ], where + '.cost' )
            if cost <= 0:
                raise SimConfigError( '%s: cost must be positive' % where )
        return cost, state

    def _parseLink( self, index: int, data: Any, graph: SimGraph ) -> LinkSpec:
        """Parse one link.

           The string and pair forms are first turned into the mapping
           form. The link starts from the link defaults (cost, state and
           shaping), with its own settings layered over them.

           :param index: position of the link in the 'links' list
           :param data: an 'a -- b' string, an [a, b] pair or a mapping
           :param graph: the graph being built (for the link defaults)
           :returns: the link
           :raises SimConfigError: for an invalid link"""
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
        defaults = graph.settings.linkDefaults
        cost, state = self._costAndState( data, defaults.cost, defaults.state,
                                          where )
        return LinkSpec( index=index,
                         a=self._parseEndpoint( data[ 'a' ], where + '.a' ),
                         b=self._parseEndpoint( data[ 'b' ], where + '.b' ),
                         id=data.get( 'id' ), cost=1 if cost is None else cost,
                         state=state or LinkState.UP,
                         tc=defaults.tc.merged( self._shaping( data, where ) ) )


# Completion passes
#
# Each pass fills in one kind of missing information, and GraphCompleter
# runs them in the order they are defined here. The order matters: a pass
# may rely on everything the passes before it filled in (for example,
# interface names need node kinds, because switch ports are numbered from
# 1; routes need addresses, which need segments). A pass never overwrites
# a value the file gave.

class CompletionPass( ABC ):
    """One step that fills in missing information on a SimGraph."""

    @abstractmethod
    def apply( self, graph: SimGraph ) -> None:
        """Complete the graph in place.

           Every value filled in is recorded with graph.log.assume().

           :param graph: graph to complete
           :raises SimConfigError: if the graph cannot be completed"""


class NodeDiscovery( CompletionPass ):
    """Creates nodes that only appear in links; flags isolated nodes.

       A file may skip the 'nodes' section entirely; every name used in a
       link becomes a node with nothing set, marked as not declared."""

    def apply( self, graph: SimGraph ) -> None:
        """Add the missing nodes and warn about nodes without links.

           :param graph: graph to complete"""
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
    """Guesses missing node kinds: from the image, then the name, then the
       degree.

       The degree of a node is its number of links. The clues are tried in
       this order and the first one that matches decides:

       1. The Docker image: a known routing software image (FRR, Quagga,
          BIRD and so on) makes a router, an Open vSwitch image a switch.
       2. The name, split into words at every character that is not a
          letter ('wan_r1' gives 'wan' and 'r'). The last word that is a
          known role word decides, so 'video_server' is a host and 'web_gw'
          is a router.
       3. The degree: a node with two or more links is taken to be a
          router (it joins networks), any other node a host."""

    #: Image names of routing software; matched anywhere in the image name
    ROUTER_IMAGE = re.compile( r'frr|quagga|bird|vyos|gobgp|openwrt|routeros' )
    #: Image names of Open vSwitch ('ovs' only as a whole word)
    SWITCH_IMAGE = re.compile( r'openvswitch|\bovs\b' )
    #: Name words that mean router, switch or host ('er' is edge router,
    #: 'cr' core router, 'pe' and 'ce' provider and customer edge, 'tor'
    #: top of rack switch, 'ue' user equipment)
    ROUTER_TOKENS = frozenset( [ 'r', 'rt', 'rtr', 'router', 'gw', 'gateway',
                                 'er', 'cr', 'pe', 'ce', 'wan', 'core', 'edge',
                                 'isp', 'fw', 'firewall', 'bgp' ] )
    SWITCH_TOKENS = frozenset( [ 's', 'sw', 'switch', 'bridge', 'tor', 'ovs' ] )
    HOST_TOKENS = frozenset( [ 'h', 'host', 'd', 'pc', 'client', 'server',
                               'srv', 'vm', 'user', 'ue', 'web', 'db', 'cam',
                               'sensor', 'node' ] )

    def apply( self, graph: SimGraph ) -> None:
        """Give every node without a kind one.

           :param graph: graph to complete"""
        for spec in graph.nodes.values():
            if spec.kind is None:
                spec.kind, reason = self.infer( spec, graph )
                graph.log.assume( spec.name, 'kind %s (%s)'
                                  % ( spec.kind.value, reason ) )

    def infer( self, spec: NodeSpec, graph: SimGraph ) -> tuple:
        """Guess the kind of one node (see the class documentation).

           :param spec: the node
           :param graph: the graph, for the node's degree
           :returns: ( NodeKind, reason ), the reason being text for the
               log such as "name contains 'gw'" """
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
    """Applies per-kind defaults and built-in requirements.

       Each node's settings are built from three layers, the later ones
       taking precedence: the built-in defaults below, the file's
       'defaults' section for the node's kind, and the node's own
       settings. The image, start, exec and the Docker and switch settings
       are layered this way. Switches lose any container settings (they
       are not containers), and routers that cannot forward are reported."""

    #: Built-in defaults per kind. Routers are privileged (so software in
    #: them, such as FRR, may change routes and interfaces) and have the
    #: sysctls they need to route.
    BUILTIN = {
        NodeKind.HOST: NodeSpec( 'host', image='alpine:3.20' ),
        NodeKind.ROUTER: NodeSpec( 'router', image='alpine:3.20', options=(
            NodeOptions(
                privileged=True,
                # ip_forward makes the kernel forward packets between
                # interfaces. rp_filter (reverse path filtering) drops a
                # packet that arrives on an interface other than the one
                # the router would use to reply; with backup routes, paths
                # can be asymmetric, so it is switched off.
                sysctls={ 'net.ipv4.ip_forward': 1,
                          'net.ipv4.conf.all.rp_filter': 0,
                          'net.ipv4.conf.default.rp_filter': 0 } ) ) ),
        NodeKind.SWITCH: NodeSpec( 'switch' ),
    }

    def apply( self, graph: SimGraph ) -> None:
        """Layer the defaults under every node's settings.

           :param graph: graph to complete (node kinds must be known)"""
        for spec in graph.nodes.values():
            builtin = self.BUILTIN[ spec.kind ]
            configured = ( graph.settings.nodeDefaults.get( spec.kind )
                           or NodeSpec( spec.kind.value ) )
            if spec.image is None and spec.kind.isL3:
                if configured.image:
                    spec.image = configured.image
                else:
                    spec.image = builtin.image
                    graph.log.assume( spec.name, 'image %s (built-in default '
                                      'for %ss)' % ( spec.image,
                                                     spec.kind.value ) )
            # Of the other settings, defaults supply start, exec and the
            # Docker/switch settings. A node's own exec list replaces the
            # default list rather than adding to it.
            spec.start = spec.start or configured.start
            spec.commands = spec.commands or list( configured.commands )
            spec.options = builtin.options.merged(
                configured.options ).merged( spec.options )
            if spec.kind is NodeKind.SWITCH:
                self._checkSwitch( spec, graph )
            if spec.kind is NodeKind.ROUTER and not (
                    spec.options.sysctls or {} ).get( 'net.ipv4.ip_forward' ):
                graph.log.warning( spec.name, 'router without '
                                   'net.ipv4.ip_forward=1 will not forward' )

    @staticmethod
    def _checkSwitch( spec: NodeSpec, graph: SimGraph ) -> None:
        """Warn about container settings on a switch and drop them.

           :param spec: the switch
           :param graph: the graph, for the log"""
        dockerOnly = spec.options.dockerSettings()
        if spec.image or dockerOnly:
            graph.log.warning( spec.name, 'switches are Open vSwitch bridges; '
                               'ignoring %s' % ', '.join(
                                   ( [ 'image' ] if spec.image else [] )
                                   + dockerOnly ) )
            spec.image = None
            spec.options = replace( spec.options,
                                    **{ name: None for name in dockerOnly } )


class SwitchIdentity( CompletionPass ):
    """Gives every switch a unique datapath ID.

       The datapath ID (dpid) is the 64-bit number that identifies a switch
       to an OpenFlow controller (ONOS shows it as of:<16 hex digits>).
       Switches without one take the first number in their name, so s3
       gets dpid 3, as Mininet does; if the name has no number, or that
       number is 0 or already taken, the lowest free number is used."""

    def apply( self, graph: SimGraph ) -> None:
        """Give every switch without a dpid one.

           :param graph: graph to complete"""
        switches = [ spec for spec in graph.nodes.values()
                     if spec.kind is NodeKind.SWITCH ]
        # Configured dpids are hexadecimal, optionally with colons between
        # byte pairs (00:00:00:00:00:00:00:01)
        used = { int( spec.options.dpid.replace( ':', '' ), 16 )
                 for spec in switches if spec.options.dpid }
        nextId = 1
        for spec in switches:
            if spec.options.dpid:
                continue
            # The number in the name is read as decimal: s12 gets dpid 12
            # (0x0c)
            digits = re.findall( r'\d+', spec.name )
            dpid = int( digits[ 0 ] ) if digits else None
            # A name number of 0 is not used either: generated dpids start
            # at 1
            if dpid is None or dpid in used or dpid == 0:
                while nextId in used:
                    nextId += 1
                reason = ( 'name has no number' if dpid is None
                           else 'dpid %d already used' % dpid )
                dpid = nextId
                graph.log.assume( spec.name, 'dpid %d (%s)' % ( dpid, reason ) )
            used.add( dpid )
            # 16 hexadecimal digits, the form Mininet and Open vSwitch use
            spec.options.dpid = '%016x' % dpid


class InterfaceNaming( CompletionPass ):
    """Names every interface <node>-eth<N>, unique and at most 15
       characters long.

       This is Mininet's naming scheme. N counts a node's interfaces in the
       order of the links: from 0 on hosts and routers, from 1 on switches,
       because Mininet numbers switch ports from 1 as OpenFlow does and
       the interface number is the port number. Names given in the file
       are kept and skipped over. Interface names live in the kernel, so
       they must be unique across the whole simulation and fit the 15
       character limit; a name that would be too long is shortened (see
       _name)."""

    def apply( self, graph: SimGraph ) -> None:
        """Name every interface without a name.

           :param graph: graph to complete (node kinds must be known)
           :raises SimConfigError: if a configured name is used twice or
               is too long"""
        # First check the configured names and reserve them, so generated
        # names avoid them
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
        # Next interface number to try, per node
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
        """Build an interface name.

           :param node: node name
           :param number: interface number
           :returns: '<node>-eth<number>' if that fits the kernel limit;
               otherwise the node name cut short followed by
               'e<number>' (node 'very_long_router' and number 2 give
               'very_long_re2'), which may clash with another name (the
               caller checks)"""
        suffix = '-eth%d' % number
        if len( node ) + len( suffix ) <= MAX_INTF_NAME_LEN:
            return node + suffix
        # 'e<N>' is 3 characters shorter than '-eth<N>'; the node name gets
        # one of them back, so the result is 2 characters under the limit
        return node[ :MAX_INTF_NAME_LEN - len( suffix ) + 1 ] + 'e%d' % number


class LinkIdentity( CompletionPass ):
    """Gives every link a unique id '<intfA>-<intfB>' (the name used by
       NetworkDegradationSimulation's web UI).

       For example, a link between d1-eth0 and er1-eth0 is called
       'd1-eth0-er1-eth0'. Since interface names are unique, so are the
       generated ids."""

    def apply( self, graph: SimGraph ) -> None:
        """Give every link without an id one.

           :param graph: graph to complete (interface names must be known)
           :raises SimConfigError: if two links have the same id"""
        seen: set = set()
        for link in graph.links:
            if not link.id:
                link.id = '%s-%s' % ( link.a.intf, link.b.intf )
            if link.id in seen:
                raise SimConfigError( 'duplicate link id %s' % link.id )
            seen.add( link.id )


class SegmentDiscovery( CompletionPass ):
    """Groups interfaces into L2 segments (one subnet each).

       Switches that are linked to each other, directly or through other
       switches, form one LAN: frames pass between them without a router.
       Every L3 interface linked to any switch of that group belongs to the
       LAN's segment. A link between two L3 nodes (with no switch) is a
       segment of its own, even when it runs parallel to another link
       between the same two nodes.

       The switch groups are found with a union-find (disjoint set)
       structure: each switch starts as its own group, every
       switch-to-switch link joins the two groups, and a group is
       represented by its root switch."""

    def apply( self, graph: SimGraph ) -> None:
        """Rebuild graph.segments from the links.

           Addresses configured on switch ports are dropped with a warning,
           since switches have no IP addresses.

           :param graph: graph to complete (node kinds and interface names
               must be known)"""
        # Union-find parent of each switch; a switch that is its own parent
        # is the root of its group
        parent = { name: name for name, spec in graph.nodes.items()
                   if spec.kind is NodeKind.SWITCH }

        def root( name: str ) -> str:
            """Find the root of a switch's group.

               Uses path halving: each visited switch is pointed at its
               grandparent, which keeps later lookups short.

               :param name: switch name
               :returns: the root switch's name"""
            while parent[ name ] != name:
                parent[ name ] = parent[ parent[ name ] ]
                name = parent[ name ]
            return name

        # Join the groups of every pair of linked switches
        for link in graph.links:
            if link.a.node in parent and link.b.node in parent:
                parent[ root( link.a.node ) ] = root( link.b.node )
        # Root switch name to the segment of its group
        lans: dict[ str, Segment ] = {}
        graph.segments = []

        def lanOf( switch: str ) -> Segment:
            """Find the segment of a switch's group, creating it on first
               use.

               :param switch: switch name
               :returns: the segment, which then lists the switch"""
            key = root( switch )
            if key not in lans:
                lans[ key ] = Segment( len( graph.segments ) )
                graph.segments.append( lans[ key ] )
            lans[ key ].switches.add( switch )
            return lans[ key ]

        # Segments are created in link order, so their order (and with it
        # the subnets they are given) follows the file
        for link in graph.links:
            switchA = link.a.node in parent
            switchB = link.b.node in parent
            if switchA and switchB:
                # A link inside a LAN: no L3 interface, only make sure both
                # switches are recorded in the LAN's segment
                lanOf( link.a.node )
                lanOf( link.b.node )
            elif switchA or switchB:
                # An access link: the L3 end joins the switch's LAN
                switch, host = ( ( link.a, link.b ) if switchA
                                 else ( link.b, link.a ) )
                if switch.ip is not None:
                    graph.log.warning( switch.intf, 'switch ports have no IP '
                                       'address; ignoring %s' % switch.ip )
                    switch.ip = None
                lanOf( switch.node ).members.append( ( link, host ) )
            else:
                # A direct link between two L3 nodes
                segment = Segment( len( graph.segments ),
                                   members=[ ( link, link.a ),
                                             ( link, link.b ) ] )
                graph.segments.append( segment )
        # The segments changed, so the cached neighbours are stale
        graph._adjacency = None


class AddressPlanning( CompletionPass ):
    """Fills in subnets and addresses for every L3 interface.

       The steps, in order:

       1. A node-level 'ip' is moved to the node's first interface that
          has no address.
       2. Addresses written without a /prefix get the prefix of the other
          addresses on their segment (or /24).
       3. A segment with any configured address takes that address's
          subnet; those subnets are reserved.
       4. Every other segment gets the first free subnet of a pool: the
          p2p pool (/30 subnets by default) for a direct router-to-router
          link, the LAN pool (/24 by default) for anything else.
       5. Interfaces still without an address get the subnet's lowest free
          addresses, routers first, so a LAN's gateway is usually .1.

       With addressing disabled, only steps 1 to 3 run."""

    def apply( self, graph: SimGraph ) -> None:
        """Give every L3 interface an address.

           :param graph: graph to complete (segments must be known)
           :raises SimConfigError: if a pool or subnet runs out of
               addresses"""
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
        """Move each node-level 'ip' to the node's first interface without
           an address (in link order).

           :param graph: graph to complete"""
        for spec in graph.nodes.values():
            if spec.ip is None:
                continue
            for endpoint in graph.endpointsOf( spec.name ):
                if endpoint.ip is None:
                    endpoint.ip = spec.ip
                    graph.log.assume( spec.name, 'node ip %s assigned to %s'
                                      % ( spec.ip, endpoint.intf ) )
                    break
            # for-else: runs only when the loop found no free interface
            else:
                graph.log.warning( spec.name, 'node ip %s unused: every '
                                   'interface has an address' % spec.ip )

    @staticmethod
    def _completePrefixes( graph: SimGraph ) -> None:
        """Give addresses written without /prefix their segment's prefix.

           The prefix comes from the first address on the segment that has
           one; if none has, /24 is assumed.

           :param graph: graph to complete"""
        for segment in graph.segments:
            known = [ ep.ip.network for _, ep in segment.members
                      if ep.ip is not None and ep.explicitPrefix ]
            for _, endpoint in segment.members:
                if endpoint.ip is None or endpoint.explicitPrefix:
                    continue
                prefix = known[ 0 ].prefixlen if known else 24
                endpoint.ip = IPv4Interface( ( endpoint.ip.ip, prefix ) )
                endpoint.explicitPrefix = True
                graph.log.assume( endpoint.intf, 'address %s (prefix %s)' % (
                    endpoint.ip, 'from %s' % segment.label() if known
                    else 'assumed /24' ) )

    @staticmethod
    def _explicitNetworks( graph: SimGraph ) -> list:
        """Derive segment networks from configured addresses.

           A segment takes the subnet of its first configured address;
           members whose addresses are in a different subnet are reported.

           :param graph: graph to complete
           :returns: the subnets taken this way, which automatic
               allocation must not overlap"""
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
        """Pick a free subnet for a segment from the matching pool.

           :param segment: a segment without a subnet
           :param graph: the graph, for the settings and node kinds
           :param used: subnets already taken; the result must not overlap
               any of them
           :returns: the first free subnet of the pool, in address order
           :raises SimConfigError: if the pool has no free subnet left"""
        settings = graph.settings
        routersOnly = all( graph.kindOf( ep.node ) is NodeKind.ROUTER
                           for _, ep in segment.members )
        if segment.isPointToPoint and routersOnly:
            pool, prefix, label = settings.p2pPool, settings.p2pPrefix, 'p2p'
        else:
            pool, prefix, label = settings.lanPool, settings.lanPrefix, 'lan'
            # grow the subnet if the LAN has more interfaces than fit:
            # n interfaces plus the network and broadcast addresses need
            # ceil(log2(n + 2)) host bits; the subnet may grow up to the
            # whole pool but no further
            needed = max( 32 - math.ceil( math.log2(
                len( segment.members ) + 2 ) ), pool.prefixlen )
            prefix = min( prefix, needed )
        # Subnets in address order; the first that overlaps nothing wins
        for candidate in pool.subnets( new_prefix=prefix ):
            if not any( candidate.overlaps( net ) for net in used ):
                graph.log.assume( segment.label(), 'subnet %s (%s pool)'
                                  % ( candidate, label ) )
                return candidate
        raise SimConfigError( '%s pool %s is exhausted' % ( label, pool ) )

    @staticmethod
    def _assignHosts( segment: Segment, graph: SimGraph ) -> None:
        """Address the segment's members that have no address.

           Routers come first, so gateways get the lowest addresses, then
           hosts, each in declaration order.

           :param segment: a segment with a subnet
           :param graph: the graph, for node kinds and the log
           :raises SimConfigError: if the subnet has too few free
               addresses"""
        taken = { ep.ip.ip for _, ep in segment.members if ep.ip is not None }
        # hosts() leaves out the network and broadcast addresses
        free = ( address for address in segment.network.hosts()
                 if address not in taken )
        # The key is False for routers and True for others, so the stable
        # sort puts routers first and keeps declaration order otherwise
        pending = sorted( ( ep for _, ep in segment.members if ep.ip is None ),
                          key=lambda ep: graph.kindOf( ep.node )
                          is not NodeKind.ROUTER )
        for endpoint in pending:
            try:
                address = next( free )
            except StopIteration:
                raise SimConfigError( '%s: subnet %s is full'
                                      % ( segment.label(), segment.network ) )
            endpoint.ip = IPv4Interface( ( address,
                                           segment.network.prefixlen ) )
            graph.log.assume( endpoint.intf, 'address %s' % endpoint.ip )


class RoutePlanning( CompletionPass ):
    """Generates static routes along shortest paths, plus loop-free
       alternates (RFC 5286) as higher-metric backups.

       Without a routing protocol, every node needs a routing table entry
       for every subnet it is not attached to. This pass computes them
       once, from the completed graph, the way a link-state routing
       protocol such as OSPF would.

       Hosts get only default routes: a host sends everything that is not
       on its own subnets to its gateway, a router it shares a segment
       with. A host with a configured gateway gets one default route
       through it. Otherwise every router the host shares a segment with
       is a gateway: the cheapest (by link cost, then declaration order)
       gets metric 1 and the others, if backup routes are enabled, get
       metrics 2, 3 and so on.

       Routers get one route per remote subnet (one whose segment the
       router is not on):

       1. Shortest paths. The cost of reaching the subnet is computed for
          every router at once, with Dijkstra's algorithm started from all
          the routers on that subnet (see SimGraph.distances). Paths pass
          only through routers, and hop costs come from the link costs.
       2. Primary route. Each neighbouring router N is a possible next hop
          (once over each segment shared with it, so parallel links count
          separately), at the cost of the hop to N plus N's own cost to
          the subnet. The cheapest one (ties go to the earlier declared
          link) becomes the route with metric 1: the next hop on a
          shortest path.
       3. Backup routes (loop-free alternates). The other neighbours are
          backup next hops, but only if they are loop-free. If the primary
          link goes down and the router R sends a packet to neighbour N
          instead, N forwards it along N's own shortest path. That is safe
          only if N's shortest path does not lead back through R, or the
          packet would bounce between them. RFC 5286 (basic loop-free
          alternates, inequality 1) guarantees this when

              dist(N, subnet) < dist(N, R) + dist(R, subnet)

          that is, when N's own way to the subnet is strictly cheaper than
          any way through R. Each loop-free neighbour gets a route with
          the next higher metric, in order of total cost.

       The kernel uses the route with the lowest metric. When a router's
       interface on the primary link goes down, Linux removes the routes
       through that interface, so the next route takes over at once. This
       protects against the failure of a router's own link; a failure
       further along the path is not noticed, since static routes do not
       react to it. Links configured 'down' count like the others, so
       routes are planned through them too (see
       Simulation._applyLinkStates for what then happens to those
       routes).

       Routes the file configures are kept: a node that already has a
       route to a subnet (or a host with a default route) gets no
       generated routes for it."""

    def apply( self, graph: SimGraph ) -> None:
        """Generate the routes of every host and router.

           Does nothing if the routing mode is not 'static'.

           :param graph: graph to complete (addresses and segments must be
               known)"""
        if graph.settings.routing is not RoutingMode.STATIC:
            graph.log.assume( 'routing', 'mode %s; only configured routes '
                              'are installed' % graph.settings.routing.value )
            return
        # Distances from each router, computed on first use by _between()
        self._betweenCache: dict[ str, dict[ str, float ] ] = {}
        for spec in graph.nodes.values():
            if spec.kind is NodeKind.HOST:
                self._planHost( spec, graph )
        for segment in graph.segments:
            if segment.network is None:
                continue
            # Every router on the segment reaches it at cost 0, so one
            # Dijkstra run from all of them gives every router's cost to
            # the segment (costs are the same in both directions)
            routers = { ep.node: 0.0 for _, ep in segment.members
                        if graph.kindOf( ep.node ) is NodeKind.ROUTER }
            distToSegment = graph.distances( routers )
            members = { ep.node for _, ep in segment.members }
            # Members reach the subnet directly (the kernel adds that route
            # when the interface gets its address), so only the other
            # routers need a route
            for spec in graph.nodes.values():
                if ( spec.kind is NodeKind.ROUTER and spec.name not in members
                     and not self._hasRoute( spec, segment.network ) ):
                    self._planRouter( spec, segment, distToSegment, graph )
        for spec in graph.nodes.values():
            generated = [ r for r in spec.routes if r.origin is RouteOrigin.AUTO ]
            if generated:
                graph.log.assume( spec.name, '%d static route(s) generated'
                                  % len( generated ) )

    @staticmethod
    def _hasRoute( spec: NodeSpec, network: IPv4Network ) -> bool:
        """Check whether a node already has a route to a network.

           :param spec: the node
           :param network: the destination (exact match, not a covering
               network)
           :returns: True if any of its routes has that destination"""
        return any( route.destination == network for route in spec.routes )

    def _between( self, source: str, graph: SimGraph ) -> dict[ str, float ]:
        """Shortest path costs from one node to every node (cached).

           :param source: the start node
           :param graph: the graph
           :returns: node name to cost, paths passing only through
               routers (see SimGraph.distances)"""
        if source not in self._betweenCache:
            self._betweenCache[ source ] = graph.distances( { source: 0.0 } )
        return self._betweenCache[ source ]

    def _planHost( self, spec: NodeSpec, graph: SimGraph ) -> None:
        """Add a host's default route(s) through its gateway router(s).

           Also fills in the host's gateway if it had none.

           :param spec: the host
           :param graph: the graph"""
        if self._hasRoute( spec, DEFAULT_ROUTE ):
            return
        if spec.gateway is not None:
            spec.routes.append( RouteSpec( DEFAULT_ROUTE, spec.gateway,
                                           origin=RouteOrigin.AUTO ) )
            return
        # Routers the host shares a segment with, cheapest first
        gateways = sorted( ( adj for adj in graph.adjacency()[ spec.name ]
                             if graph.kindOf( adj.peer.node ) is NodeKind.ROUTER
                             and adj.peer.ip is not None ),
                           key=lambda adj: ( adj.cost, adj.order ) )
        if not graph.settings.backupRoutes:
            gateways = gateways[ :1 ]
        for metric, adj in enumerate( gateways, start=1 ):
            spec.routes.append( RouteSpec( DEFAULT_ROUTE, adj.peer.ip.ip,
                                           adj.local.intf, metric,
                                           RouteOrigin.AUTO ) )
        if gateways:
            spec.gateway = gateways[ 0 ].peer.ip.ip
            graph.log.assume( spec.name, 'gateway %s (%s)'
                              % ( spec.gateway, gateways[ 0 ].peer.node ) )

    def _planRouter( self, spec: NodeSpec, segment: Segment,
                     distToSegment: dict, graph: SimGraph ) -> None:
        """Add the routes from one router to one remote segment.

           The primary route goes to the neighbour on a shortest path;
           backup routes go to the other neighbours that are loop-free
           alternates (see the class documentation).

           :param spec: the router (not on the segment)
           :param segment: the destination segment
           :param distToSegment: every node's shortest path cost to the
               segment
           :param graph: the graph"""
        if spec.name not in distToSegment:
            graph.log.warning( spec.name, 'no path to %s (%s)' % (
                segment.network, segment.label() ) )
            return
        # dist(R, subnet) in the loop-free condition
        own = distToSegment[ spec.name ]
        # ( total cost, tie-break order, adjacency, loop-free ) per usable
        # neighbour
        candidates = []
        for adj in graph.adjacency()[ spec.name ]:
            peer = adj.peer.node
            # Only a router with an address and a path to the subnet can be
            # a next hop
            if ( graph.kindOf( peer ) is not NodeKind.ROUTER
                 or peer not in distToSegment or adj.peer.ip is None ):
                continue
            total = adj.cost + distToSegment[ peer ]
            # RFC 5286 inequality 1:
            # dist(N, subnet) < dist(N, R) + dist(R, subnet)
            loopFree = distToSegment[ peer ] < (
                self._between( peer, graph ).get( spec.name, math.inf ) + own )
            candidates.append( ( total, adj.order, adj, loopFree ) )
        candidates.sort( key=lambda c: ( c[ 0 ], c[ 1 ] ) )
        # The cheapest candidate is the primary next hop (it is always
        # loop-free, since link costs are positive)
        chosen = candidates[ :1 ]
        if graph.settings.backupRoutes:
            chosen += [ c for c in candidates[ 1: ] if c[ 3 ] ]
        # Metrics 1, 2, 3 and so on, in order of total cost
        for metric, ( _total, _order, adj, _lfa ) in enumerate( chosen, 1 ):
            spec.routes.append( RouteSpec( segment.network, adj.peer.ip.ip,
                                           adj.local.intf, metric,
                                           RouteOrigin.AUTO ) )


class GraphCompleter( object ):
    """Runs the completion passes in order. Add or replace passes to change
       how blanks are filled in."""

    #: The passes run by default, in order; each relies on the ones before
    #: it (see the comment above CompletionPass)
    DEFAULT_PASSES = ( NodeDiscovery, KindInference, NodeDefaults,
                       SwitchIdentity, InterfaceNaming, LinkIdentity,
                       SegmentDiscovery, AddressPlanning, RoutePlanning )

    def __init__( self, passes: Iterable[ CompletionPass ] | None = None
                  ) -> None:
        """Create a completer.

           :param passes: pass instances to run, in order (default: one
               instance of each of DEFAULT_PASSES)"""
        self.passes = ( list( passes ) if passes is not None
                        else [ cls() for cls in self.DEFAULT_PASSES ] )

    def complete( self, graph: SimGraph ) -> SimGraph:
        """Fill in every blank in the graph, in place.

           :param graph: a graph from ConfigParser.parse()
           :returns: the same graph, completed
           :raises SimConfigError: if the graph cannot be completed"""
        for completionPass in self.passes:
            completionPass.apply( graph )
        return graph


# Building and running
#
# Turning a completed graph into a Containernet network needs root and
# Open vSwitch, unlike everything above.

class Simulation( object ):
    """A built Containernet network together with its graph.

       :ivar graph: the completed graph it was built from
       :ivar net: the Containernet network
       :ivar nodes: node name to Mininet node (Docker container or switch)
       :ivar links: link id to Mininet link
       :ivar started: whether start() has run (and stop() has not)"""

    def __init__( self, graph: SimGraph, net, nodes: dict,
                  links: dict ) -> None:
        """Wrap a built network; normally done by SimulationBuilder.

           :param graph: completed SimGraph
           :param net: Containernet instance
           :param nodes: node name to Mininet node
           :param links: link id to Mininet link"""
        self.graph = graph
        self.net = net
        self.nodes = nodes
        self.links = links
        self.started = False

    def node( self, name: str ):
        """Look up a node.

           :param name: node name
           :returns: the Mininet node with this name
           :raises KeyError: if there is none"""
        return self.nodes[ name ]

    def link( self, linkId: str ):
        """Look up a link.

           :param linkId: link id (see LinkIdentity)
           :returns: the Mininet link with this id
           :raises KeyError: if there is none"""
        return self.links[ linkId ]

    def start( self ) -> Simulation:
        """Start the network and set it up.

           In order: start the network, install routes, apply link states
           and run the configured commands. Calling it again does nothing.

           :returns: self, so it can be chained"""
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
        """Open the interactive Mininet CLI; returns when the user leaves
           it."""
        from mininet.cli import CLI
        CLI( self.net )

    def __enter__( self ) -> Simulation:
        """Start on entering a with-block.

           :returns: self, started"""
        return self.start()

    def __exit__( self, *_exc ) -> None:
        """Stop on leaving a with-block (also after an exception, which
           then propagates).

           :param _exc: exception type, value and traceback (unused)"""
        self.stop()

    def _installRoutes( self ) -> None:
        """Install the configured and generated routes with iproute2.

           Failures are reported as warnings; the other routes are still
           installed."""
        for name, spec in self.graph.nodes.items():
            if not spec.routes:
                continue
            node = self.nodes[ name ]
            if any( route.destination == DEFAULT_ROUTE
                    for route in spec.routes ):
                # replaces Docker's default route via its bridge network
                # (every container also has an eth0 on Docker's own
                # network, with a default route through it)
                node.netCmd( 'ip', 'route', 'del', 'default' )
            for route in spec.routes:
                # netCmd runs the command in the node's network namespace
                result = node.netCmd( *route.argv() )
                # 'ip route add' prints nothing on success, so any output
                # is an error message
                if result.strip():
                    warn( '*** simconfig %s: %s failed: %s' % (
                        name, ' '.join( route.argv() ), result ) )

    def _applyLinkStates( self ) -> None:
        """Bring links configured as 'down' down (after routing).

           This runs after _installRoutes() because the kernel refuses
           routes through an interface that is down. Setting an interface
           down then makes Linux delete the routes through it, and nothing
           here puts them back if the link is later brought up again
           (network_builder/topology.py does restore them)."""
        for link in self.graph.links:
            if link.state is LinkState.DOWN:
                mnLink = self.links[ link.id ]
                mnLink.intf1.set_state( 'down' )
                mnLink.intf2.set_state( 'down' )

    def _runCommands( self ) -> None:
        """Run 'exec' commands and image entrypoints ('start').

           Each node's exec commands run first, one at a time in its shell;
           then, if 'start' is set, the image's ENTRYPOINT and CMD are
           started in the background."""
        for name, spec in self.graph.nodes.items():
            node = self.nodes[ name ]
            for command in spec.commands:
                node.cmd( command )
            if spec.start:
                node.start()


class SimulationBuilder( object ):
    """Creates Containernet objects from a completed SimGraph.

       Hosts and routers become Docker containers, switches Open vSwitch
       switches, and links TCLinks (veth pairs whose interfaces apply tc
       shaping)."""

    #: Docker settings passed to addDocker() under the same name
    DOCKER_PASSTHROUGH = ( 'volumes', 'privileged', 'cpus', 'mem_limit',
                           'memswap_limit', 'cpu_shares', 'cpu_quota',
                           'cpu_period', 'cpuset_cpus', 'dns', 'devices',
                           'cap_add' )

    def __init__( self, graph: SimGraph ) -> None:
        """Create a builder.

           :param graph: completed graph (see GraphCompleter)"""
        self.graph = graph

    def build( self ) -> Simulation:
        """Create the network without starting it. Requires root.

           The containers are created (and running) and the links wired,
           but the switches are not started, no routes are installed and
           no commands run until Simulation.start(). If anything fails, what
           was created so far is removed again.

           :returns: the simulation, not yet started
           :raises SimConfigError: for a controller that does not run
               elsewhere (only RemoteControllerSpec is supported here)"""
        # Imported here so parsing works without root or Open vSwitch
        from mininet.link import TCIntf, TCLink
        from mininet.net import Containernet
        from mininet.node import OVSBridge, OVSSwitch, RemoteController

        # No default controller: controllers come only from the graph.
        # TCIntf and TCLink make every interface able to apply shaping.
        net = Containernet( controller=None, intf=TCIntf, link=TCLink )
        for controller in self.graph.controllers:
            if not isinstance( controller, RemoteControllerSpec ):
                raise SimConfigError( 'controller %s: only controllers '
                                      'running elsewhere (ip, port) can be '
                                      'used here' % controller.name )
            net.addController( controller.name, controller=RemoteController,
                               ip=controller.ip, port=controller.port )
        # With controllers, switches are OpenFlow switches they manage;
        # without, OVSBridge makes them standalone learning switches (they
        # learn which port leads to which MAC address by themselves)
        switchCls = OVSSwitch if self.graph.controllers else OVSBridge
        nodes = {}
        try:
            for name, spec in self.graph.nodes.items():
                if spec.kind is NodeKind.SWITCH:
                    nodes[ name ] = net.addSwitch(
                        name, cls=switchCls, **self.switchArgs( spec ) )
                else:
                    # ip=None: no automatic Mininet address (10.0.0.x); the
                    # addresses come with the links. rm=True: remove a
                    # leftover container of the same name first.
                    kwargs = { 'dimage': spec.image, 'ip': None, 'rm': True }
                    kwargs.update( self.dockerArgs( spec ) )
                    nodes[ name ] = net.addDocker( name, **kwargs )
            links = {}
            for link in self.graph.links:
                links[ link.id ] = net.addLink(
                    nodes[ link.a.node ], nodes[ link.b.node ], cls=TCLink,
                    intfName1=link.a.intf, intfName2=link.b.intf,
                    params1=self._intfParams( link, 'a' ),
                    params2=self._intfParams( link, 'b' ) )
        # BaseException so that even Ctrl-C (KeyboardInterrupt) during the
        # build removes the containers created so far
        except BaseException:
            net.stop()
            raise
        return Simulation( self.graph, net, nodes, links )

    @staticmethod
    def _intfParams( link: LinkSpec, side: str ) -> dict:
        """Interface parameters for one side of a link.

           :param link: the link
           :param side: 'a' or 'b'
           :returns: keyword arguments for the interface (params1 or
               params2 of addLink()): that end's shaping options and its
               address as 'ip'"""
        endpoint = link.a if side == 'a' else link.b
        params = link.shaping( side ).params()
        if endpoint.ip is not None:
            params[ 'ip' ] = str( endpoint.ip )
        return params

    def dockerArgs( self, spec: NodeSpec ) -> dict:
        """Translate a node's settings into addDocker() keyword arguments.

           The raw 'params' come first; named settings override them.

           :param spec: a host or router
           :returns: keyword arguments for Containernet.addDocker() (the
               image is added by the caller)"""
        options = spec.options
        args = copy.deepcopy( options.params or {} )
        if options.ports:
            # Docker's API takes the exposed container ports and, apart,
            # a mapping from container port to host port (None: any free
            # host port)
            args[ 'ports' ] = [ port.containerPort for port in options.ports ]
            args[ 'port_bindings' ] = { port.containerPort: port.hostPort
                                        for port in options.ports }
        if options.sysctls is not None:
            # Docker's API wants sysctl values as strings
            args[ 'sysctls' ] = { key: str( value ) for key, value
                                  in options.sysctls.items() }
        if options.environment is not None:
            args[ 'environment' ] = dict( options.environment )
        # Containernet calls the container's command dcmd and its shell
        # dshell
        if options.cmd is not None:
            args[ 'dcmd' ] = options.cmd
        if options.shell is not None:
            args[ 'dshell' ] = options.shell
        for name in self.DOCKER_PASSTHROUGH:
            if getattr( options, name ) is not None:
                args[ name ] = copy.deepcopy( getattr( options, name ) )
        return args

    def switchArgs( self, spec: NodeSpec ) -> dict:
        """Translate a node's settings into addSwitch() keyword arguments.

           The raw 'params' come first; named settings override them.

           :param spec: a switch
           :returns: keyword arguments for Containernet.addSwitch() (the
               class is added by the caller)"""
        options = spec.options
        args = copy.deepcopy( options.params or {} )
        # Controllers such as ONOS expect OpenFlow 1.3
        if self.graph.controllers:
            args.setdefault( 'protocols', 'OpenFlow13' )
        if options.dpid is not None:
            args[ 'dpid' ] = options.dpid
        if options.protocols is not None:
            args[ 'protocols' ] = options.protocols
        if options.fail_mode is not None:
            # Mininet's name for the setting
            args[ 'failMode' ] = options.fail_mode.value
        return args


# Convenience API

def loadGraph( source: str | Path | dict,
               log: InferenceLog | None = None ) -> SimGraph:
    """Parse and complete a configuration (no root needed).

       :param source: path to a YAML/JSON file, or an already loaded
           mapping
       :param log: inference log to use (a new one, printing through
           mininet.log, if omitted)
       :returns: completed SimGraph
       :raises SimConfigError: for invalid configurations
       :raises OSError: if the file cannot be read"""
    log = log or InferenceLog()
    parser = ConfigParser( log )
    data = source if isinstance( source, dict ) else parser.readFile( source )
    return GraphCompleter().complete( parser.parse( data ) )


def loadSimulation( source: str | Path | dict,
                    log: InferenceLog | None = None ) -> Simulation:
    """Parse, complete and build a simulation (requires root).

       Use start() and stop(), or a with-block, to run it.

       :param source: path to a YAML/JSON file, or an already loaded
           mapping
       :param log: inference log to use
       :returns: the built simulation, not yet started
       :raises SimConfigError: for invalid configurations
       :raises OSError: if the file cannot be read"""
    return SimulationBuilder( loadGraph( source, log ) ).build()


def main( argv: list | None = None ) -> int:
    """Command line entry point; see the module documentation.

       Without --dry-run, the network is built, started, left to the
       Mininet CLI (unless --no-cli) and stopped again.

       :param argv: command line arguments (default: sys.argv[1:])
       :returns: the exit status: 0 on success, 2 if the configuration
           could not be loaded"""
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
