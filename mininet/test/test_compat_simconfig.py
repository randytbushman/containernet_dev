#!/usr/bin/env python3
"""Unit tests for simconfig's configuration values (no root or Docker needed).

mininet/simconfig.py turns a YAML topology description into dataclasses,
completes it (inferring node kinds, addresses and routes) and builds the
Containernet network from it. These tests check the value classes Shaping
(per-interface traffic control options), NodeOptions (Docker and switch
settings) and PortBinding (published ports): parsing, validation, layering
of defaults and conversion back to configuration form. They also run whole
configurations through loadGraph() (parsing and completion) and check the
keyword arguments SimulationBuilder would pass to Containernet, without
building a network.

Run from the containernet directory with::

    python3 -m unittest mininet.test.test_compat_simconfig
"""

import inspect
import unittest
from dataclasses import fields

from mininet.link import TCIntf
from mininet.simconfig import ( InferenceLog, NodeOptions, PortBinding,
                                RemoteControllerSpec, Shaping, SimConfigError,
                                SimulationBuilder, loadGraph )


def graph( data: dict ):
    """Parse and complete a configuration quietly.

       :param data: configuration as loaded from YAML
       :returns: the completed SimGraph; inference notes are collected but
                 not printed"""
    return loadGraph( data, InferenceLog( echo=False ) )


class testShaping( unittest.TestCase ):
    """Shaping: TCIntf options with validation and layering."""

    def testFieldsAreTCIntfOptions( self ):
        """Every Shaping field is a keyword argument of TCIntf.config(), so
           Shaping.params() can be passed to it directly."""
        accepted = set( inspect.signature( TCIntf.config ).parameters )
        self.assertLessEqual( { f.name for f in fields( Shaping ) }, accepted )

    def testParse( self ):
        """Configuration values are normalised: a bare number of
           milliseconds becomes '40ms', '2%' becomes 2 and '10' becomes
           10."""
        shaping = Shaping.fromDict( { 'delay': 40, 'jitter': '250us',
                                      'loss': '2%', 'bw': '10',
                                      'max_queue_size': 100,
                                      'use_tbf': True }, 'test' )
        self.assertEqual( shaping, Shaping( delay='40ms', jitter='250us',
                                            loss=2, bw=10, max_queue_size=100,
                                            use_tbf=True ) )

    def testRejects( self ):
        """Invalid durations, out-of-range loss, non-numbers, non-integer
           queue sizes, non-boolean flags and unknown options all raise
           SimConfigError."""
        for bad in ( { 'delay': 'fast' }, { 'loss': 101 }, { 'bw': 'x' },
                     { 'max_queue_size': 1.5 }, { 'use_tbf': 'yes' },
                     { 'colour': 'red' } ):
            with self.subTest( bad ), self.assertRaises( SimConfigError ):
                Shaping.fromDict( bad, 'test' )

    def testMergeAndForms( self ):
        """merged() lets the override's set options win and keeps the
           rest; params() lists only set options; toDict() puts the basic
           options at the top and the others under 'tc'; an empty Shaping
           is false; milliseconds() converts durations."""
        link = Shaping( delay='10ms', loss=1 )
        end = Shaping( loss=2, use_hfsc=True )
        merged = link.merged( end )
        self.assertEqual( merged, Shaping( delay='10ms', loss=2,
                                           use_hfsc=True ) )
        self.assertEqual( merged.params(),
                          { 'delay': '10ms', 'loss': 2, 'use_hfsc': True } )
        self.assertEqual( merged.toDict(), { 'delay': '10ms', 'loss': 2,
                                             'tc': { 'use_hfsc': True } } )
        self.assertFalse( Shaping() )
        self.assertTrue( merged )
        self.assertEqual( Shaping.milliseconds( '1.5s' ), 1500 )


class testNodeOptions( unittest.TestCase ):
    """NodeOptions: Docker and switch settings."""

    def testParseAndForms( self ):
        """'env' (a list of KEY=value) and 'environment' (a mapping) are
           combined, the latter winning; values become strings and a bare
           KEY gets ''. Ports parse as CONTAINER or 'HOST:CONTAINER', a
           single string becomes a list, and toDict() gives the
           configuration form back."""
        options = NodeOptions.fromDict( {
            'env': [ 'A=1', 'B' ], 'environment': { 'A': 2 },
            'ports': [ 80, '8080:8081' ], 'cap_add': 'NET_ADMIN',
            'privileged': True, 'cpus': '0.5', 'mem_limit': '256m' }, 'n' )
        self.assertEqual( options.environment, { 'A': '2', 'B': '' } )
        self.assertEqual( options.ports, [ PortBinding( 80 ),
                                           PortBinding( 8081, 8080 ) ] )
        self.assertEqual( options.cap_add, [ 'NET_ADMIN' ] )
        self.assertEqual( options.toDict()[ 'ports' ], [ 80, '8080:8081' ] )
        self.assertEqual( options.toDict()[ 'env' ], { 'A': '2', 'B': '' } )

    def testMerge( self ):
        """Layering merges mappings, replaces lists, keeps settings the
           override leaves unset, and does not modify the base."""
        base = NodeOptions( sysctls={ 'a': 1 }, privileged=True, dns=[ 'x' ] )
        override = NodeOptions( sysctls={ 'b': 2 }, dns=[ 'y' ] )
        merged = base.merged( override )
        self.assertEqual( merged.sysctls, { 'a': 1, 'b': 2 } )   # merged
        self.assertEqual( merged.dns, [ 'y' ] )                   # replaced
        self.assertTrue( merged.privileged )                      # kept
        self.assertEqual( base.sysctls, { 'a': 1 } )              # untouched

    def testRejects( self ):
        """Non-numeric and out-of-range ports, non-boolean flags,
           non-integer CPU shares, an unknown fail_mode and sysctls that
           are not a mapping raise SimConfigError."""
        for bad in ( { 'ports': [ 'http' ] }, { 'ports': [ 70000 ] },
                     { 'privileged': 'yes' }, { 'cpu_shares': 'many' },
                     { 'fail_mode': 'open' }, { 'sysctls': [ 'x' ] } ):
            with self.subTest( bad ), self.assertRaises( SimConfigError ):
                NodeOptions.fromDict( bad, 'n' )


class testGraph( unittest.TestCase ):
    """Configuration values through the parser, completion and builder."""

    def testDefaultsLayerUnderNodes( self ):
        """Per-kind defaults are layered under a node's own settings and
           reach addDocker() as strings. r1, inferred to be a router from
           its name, gets the built-in router settings (privileged, IPv4
           forwarding)."""
        g = graph( { 'defaults': { 'host': { 'sysctls': { 'a': 1 },
                                             'env': { 'X': 1 } } },
                     'nodes': { 'h1': { 'kind': 'host',
                                        'sysctls': { 'b': 2 } } },
                     'links': [ 'h1 -- r1' ] } )
        args = SimulationBuilder( g ).dockerArgs( g.nodes[ 'h1' ] )
        self.assertEqual( args[ 'sysctls' ], { 'a': '1', 'b': '2' } )
        self.assertEqual( args[ 'environment' ], { 'X': '1' } )
        router = SimulationBuilder( g ).dockerArgs( g.nodes[ 'r1' ] )
        self.assertTrue( router[ 'privileged' ] )
        self.assertEqual( router[ 'sysctls' ][ 'net.ipv4.ip_forward' ], '1' )

    def testPerEndShaping( self ):
        """Shaping is layered as link defaults, then the link's own
           options, then one end's options, which apply to that end only.
           The interface parameters include the completed address."""
        g = graph( { 'defaults': { 'link': { 'loss': 1 } },
                     'links': [ { 'a': { 'node': 'h1', 'bw': 5 },
                                  'b': 'r1', 'delay': 20 } ] } )
        link = g.links[ 0 ]
        self.assertEqual( link.shaping( 'a' ),
                          Shaping( delay='20ms', loss=1, bw=5 ) )
        self.assertEqual( link.shaping( 'b' ), Shaping( delay='20ms', loss=1 ) )
        params = SimulationBuilder._intfParams( link, 'a' )
        self.assertEqual( params[ 'bw' ], 5 )
        self.assertIn( 'ip', params )
        # The completed form keeps the end's own shaping on the end
        self.assertEqual( link.toDict()[ 'a' ][ 'bw' ], 5 )

    def testSwitchesAndControllers( self ):
        """A controller gets the default OpenFlow port 6653. A switch (an
           Open vSwitch bridge, not a container) drops container-only
           settings such as cmd, and with a controller present it is set
           up for OpenFlow 1.3 with its dpid (OpenFlow datapath ID) and
           fail mode."""
        g = graph( { 'controllers': [ { 'name': 'c0', 'ip': '10.0.0.9' } ],
                     'nodes': { 's1': { 'kind': 'switch', 'cmd': 'x',
                                        'fail_mode': 'standalone' } },
                     'links': [ 'h1 -- s1' ] } )
        self.assertEqual( g.controllers,
                          [ RemoteControllerSpec( 'c0', '10.0.0.9', 6653 ) ] )
        switch = g.nodes[ 's1' ]
        self.assertIsNone( switch.options.cmd )    # container-only: dropped
        self.assertEqual( SimulationBuilder( g ).switchArgs( switch ),
                          { 'protocols': 'OpenFlow13', 'dpid': switch.options.dpid,
                            'failMode': 'standalone' } )
        self.assertEqual( g.toDict()[ 'controllers' ],
                          [ { 'name': 'c0', 'ip': '10.0.0.9', 'port': 6653 } ] )

    def testRoundTrip( self ):
        """The completed configuration written by toDict() loads again to
           the same result, and a node's port list replaces the default
           one rather than adding to it."""
        data = { 'defaults': { 'host': { 'env': [ 'A=1' ], 'ports': [ 80 ] },
                               'link': { 'delay': 5 } },
                 'nodes': { 'web': { 'kind': 'host', 'ports': [ '8080:80' ] } },
                 'links': [ { 'a': { 'node': 'web', 'loss': 1 }, 'b': 'r1',
                              'tc': { 'use_tbf': True } } ] }
        first = graph( data )
        second = graph( first.toDict() )
        self.assertEqual( first.toDict(), second.toDict() )
        self.assertEqual( second.nodes[ 'web' ].options.ports,
                          [ PortBinding( 80, 8080 ) ] )


if __name__ == '__main__':
    unittest.main()
