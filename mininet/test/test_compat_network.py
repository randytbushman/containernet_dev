"""
End-to-end topology tests across image families. Needs root and Docker.

These tests build small Containernet networks without an SDN controller
and check real traffic between containers. They cover veth links (virtual
Ethernet pairs) into containers; nsenter-based configuration, which runs
the host's ip and tc inside a container's network namespace so images
without networking tools work; addresses given on links surviving the
automatic host configuration; neighbour and route helpers; routing with
failover to a backup link; tc shaping and its reset; and interface up and
down. The routed test mirrors the NetworkDegradationSimulation "simple
redundancy" experiment (topologies/simple_redundancy.yaml in the main
repository).

Run from the containernet directory with::

    sudo python3 -m unittest discover -s mininet/test -p 'test_compat_network.py' -v
"""

import subprocess
import unittest

from compat_support import ( DockerTestCase, BASH_IPROUTE_IMAGE,
                             BASH_MINIMAL_IMAGE, BUSYBOX_IMAGE,
                             containerExists, pingOnce, pingRttMs )
from mininet.link import Intf, TCIntf, TCLink
from mininet.log import setLogLevel
from mininet.net import Containernet
from mininet.netexec import NamespaceRunner

# Docker sysctls for containers acting as routers: enable IPv4 forwarding
ROUTER_SYSCTLS = { 'net.ipv4.ip_forward': '1' }


class NetworkTestCase( DockerTestCase ):
    """Base class: creates a controller-less Containernet per test and
       stops it afterwards. Requires root (needsRoot)."""

    needsRoot = True
    #: Interface class used for every interface of the network
    intfClass = Intf

    def setUp( self ):
        """Create an empty network."""
        setLogLevel( 'warning' )
        self.net = Containernet( controller=None, intf=self.intfClass )

    def tearDown( self ):
        """Stop the network, then remove anything left behind."""
        try:
            self.net.stop()
        finally:
            super().tearDown()

    def addHost( self, name, image, **params ):
        """Add a Docker host, replacing any stale container of that name.

           :param name: node name (the container is 'mn.NAME')
           :param image: Docker image reference
           :param params: further addDocker() keyword arguments
           :returns: the Docker node"""
        return self.net.addDocker( name, dimage=image, rm=True, **params )


class testMixedImages( NetworkTestCase ):
    """Hosts from all three image families on direct links.

       alp (BusyBox) is linked to ubu (Ubuntu with iproute2) on 10.1.0.0/24
       and to deb (Debian slim, no networking tools) on 10.2.0.0/24."""

    def setUp( self ):
        """Build and start the three-host network."""
        super().setUp()
        self.alp = self.addHost( 'alp', BUSYBOX_IMAGE )
        self.ubu = self.addHost( 'ubu', BASH_IPROUTE_IMAGE )
        self.deb = self.addHost( 'deb', BASH_MINIMAL_IMAGE )
        self.net.addLink( self.alp, self.ubu,
                          params1={ 'ip': '10.1.0.1/24' },
                          params2={ 'ip': '10.1.0.2/24' } )
        self.net.addLink( self.alp, self.deb,
                          params1={ 'ip': '10.2.0.1/24' },
                          params2={ 'ip': '10.2.0.2/24' } )
        self.net.start()

    def testDockerNodesUseNsenter( self ):
        """As root, every Docker node configures its network through
           nsenter."""
        for host in ( self.alp, self.ubu, self.deb ):
            self.assertIsInstance( host.netRunner, NamespaceRunner )
            self.assertTrue( host.netRunner.usable(), host.name )

    def testImageWithoutNetworkingToolsIsConfigured( self ):
        """An image without ip, ifconfig and tc still gets an interface
           that is up, addressed and reachable."""
        self.assertEqual( self.deb.cmd( 'command -v ip ifconfig tc' ), '' )
        self.assertEqual( self.deb.cmd( 'cat /sys/class/net/deb-eth0/operstate' )
                          .strip(), 'up' )
        self.assertEqual( self.deb.defaultIntf().updateIP(), '10.2.0.2' )
        self.assertTrue( pingOnce( self.alp, '10.2.0.2' ) )

    def testBashImageReachable( self ):
        """Pings work in both directions between BusyBox and Ubuntu."""
        self.assertTrue( pingOnce( self.alp, '10.1.0.2' ) )
        self.assertTrue( pingOnce( self.ubu, '10.1.0.1' ) )

    def testLinkAddressIsNotOverwritten( self ):
        """Addresses given on links are kept, and no automatic 10.0.0.x
           address is added to the default interface."""
        # The first link's address must survive configHosts()
        for host, ip in ( ( self.alp, '10.1.0.1' ), ( self.ubu, '10.1.0.2' ),
                          ( self.deb, '10.2.0.2' ) ):
            self.assertEqual( host.IP(), ip )
            addresses = host.netCmd( 'ip -4 -o address show dev',
                                     host.defaultIntf() )
            self.assertIn( ip + '/24', addresses )
            self.assertNotIn( '10.0.0.', addresses )

    def testAutomaticAddressWithoutLinkAddress( self ):
        """A host whose link has no address gets Mininet's automatic
           10.0.0.x address from configHosts()."""
        extra = self.addHost( 'ext', BUSYBOX_IMAGE )
        self.net.addLink( self.alp, extra )
        self.net.configHosts()
        self.assertTrue( extra.IP().startswith( '10.0.0.' ) )

    def testNeighbourAndRouteHelpers( self ):
        """setARP(), setHostRoute() and setDefaultRoute() install the
           expected neighbour (ARP) entry and routes."""
        alpIntf = self.alp.intf( 'alp-eth1' )
        alpIntf.updateMAC()
        self.deb.setARP( '10.2.0.1', alpIntf.MAC() )
        self.assertIn( alpIntf.MAC(), self.deb.netCmd( 'ip neigh show 10.2.0.1' ) )
        self.deb.setHostRoute( '10.1.0.1', 'deb-eth0' )
        self.assertIn( '10.1.0.1 dev deb-eth0',
                       self.deb.netCmd( 'ip route show 10.1.0.1' ) )
        self.deb.setDefaultRoute( 'dev deb-eth0 via 10.2.0.1' )
        self.assertIn( 'default via 10.2.0.1',
                       self.deb.netCmd( 'ip route show default' ) )

    def testInterfaceStateControlsConnectivity( self ):
        """An interface set down stops traffic; set up again, traffic
           flows."""
        intf = self.ubu.defaultIntf()
        intf.set_state( 'down' )
        self.assertEqual( intf.get_state(), 'down' )
        self.assertFalse( pingOnce( self.alp, '10.1.0.2', timeout=1 ) )
        intf.set_state( 'up' )
        self.assertEqual( intf.get_state(), 'up' )
        self.assertTrue( pingOnce( self.alp, '10.1.0.2' ) )

    def testConfigLinkStatus( self ):
        """Containernet.configLinkStatus() switches both ends of the links
           between two nodes."""
        self.net.configLinkStatus( 'alp', 'deb', 'down' )
        self.assertEqual( self.deb.defaultIntf().get_state(), 'down' )
        self.net.configLinkStatus( 'alp', 'deb', 'up' )
        self.assertTrue( pingOnce( self.alp, '10.2.0.2' ) )

    def testStopRemovesContainersAndLinks( self ):
        """stop() removes the containers and leaves no veth interfaces of
           the nodes on the host."""
        self.net.stop()
        for name in ( 'mn.alp', 'mn.ubu', 'mn.deb' ):
            self.assertFalse( containerExists( name ) )
        hostLinks = subprocess.run( [ 'ip', '-o', 'link', 'show' ],
                                    capture_output=True, text=True ).stdout
        for prefix in ( 'alp-eth', 'ubu-eth', 'deb-eth' ):
            self.assertNotIn( prefix, hostLinks )
        self.net = Containernet( controller=None )   # tearDown stops this


class testTrafficShaping( NetworkTestCase ):
    """tc shaping through the API the web GUI uses (intf.config).

       ta (BusyBox) and tb (Debian slim, so tc can only run from the host)
       share one TCLink on 10.3.0.0/24. netem, the kernel's network
       emulator, adds the delay and loss."""

    intfClass = TCIntf

    def setUp( self ):
        """Build and start the two-host network."""
        super().setUp()
        self.a = self.addHost( 'ta', BUSYBOX_IMAGE )
        self.b = self.addHost( 'tb', BASH_MINIMAL_IMAGE )   # no tc inside
        self.link = self.net.addLink( self.a, self.b, cls=TCLink,
                                      params1={ 'ip': '10.3.0.1/24' },
                                      params2={ 'ip': '10.3.0.2/24' } )
        self.net.start()

    def shape( self, **params ):
        """Apply the same shaping to both ends, like set_link_tc().

           :param params: TCIntf.config() keyword arguments"""
        self.link.intf1.config( **params )
        self.link.intf2.config( **params )

    def testDelayAppliesAndResets( self ):
        """50 ms on each end adds about 100 ms to the round trip, and
           shaping without parameters removes the netem qdisc again."""
        baseline = pingRttMs( self.a, '10.3.0.2' )
        self.assertIsNotNone( baseline )
        self.shape( delay='50ms' )
        shaped = pingRttMs( self.a, '10.3.0.2' )
        self.assertGreaterEqual( shaped, 95 )
        # No parameters: back to the kernel's default qdisc
        self.shape()
        self.assertLess( pingRttMs( self.a, '10.3.0.2' ), 40 )
        self.assertNotIn( 'netem', self.b.netCmd( 'tc qdisc show dev tb-eth0' ) )

    def testLossCanBeRemoved( self ):
        """100 percent loss blocks traffic, and loss=0 restores it."""
        self.shape( loss=100 )
        self.assertFalse( pingOnce( self.a, '10.3.0.2', timeout=1 ) )
        self.shape( loss=0 )
        self.assertTrue( pingOnce( self.a, '10.3.0.2' ) )

    def testStringParametersFromJson( self ):
        """String values, as the web GUI sends them in JSON, are accepted:
           the bandwidth limit (HTB) and the delay (netem) are both
           installed."""
        self.shape( delay='20ms', loss='0%', bw='10' )
        qdisc = self.a.netCmd( 'tc qdisc show dev ta-eth0' )
        self.assertIn( 'htb', qdisc )
        self.assertIn( 'delay 20', qdisc )

    def testReshapingKeepsInterfaceDown( self ):
        """Changing the shaping of a down interface does not bring it
           up."""
        self.link.intf2.set_state( 'down' )
        self.link.intf2.config( delay='10ms' )
        self.assertEqual( self.link.intf2.get_state(), 'down' )


class testRoutedRedundancy( NetworkTestCase ):
    """Static routing with a backup link, like the "simple redundancy"
       experiment.

       Host d1 (192.168.1.0/24) connects to edge router er1, and host d2
       (192.168.2.0/24) to edge router er2. er1 reaches the WAN router wan
       over two links: the primary (10.0.1.0/30, er1-eth1, route metric 1)
       and the backup (20.0.1.0/30, er1-eth2, metric 2). er2 reaches wan
       over 10.0.2.0/30. When the primary goes down, the kernel drops its
       routes and traffic uses the backup.

       Mirrors topologies/simple_redundancy.yaml in the main repository
       using Ubuntu hosts (as the project does), BusyBox routers (the
       project uses FRR), an ifconfig override like the project's, and
       TCIntf as the network-wide interface class."""

    intfClass = TCIntf

    @staticmethod
    def ipLinkConf( intf, *args ):
        """Replace ifconfig with 'ip link set', as
           network_builder/topology.py does.

           Unlike the project's version, which uses intf.netCmd(), this
           runs the command through the node's shell (intf.cmd()), so it
           relies on the image's ip binary.

           :param intf: interface to configure
           :param args: ifconfig-style arguments such as 'up' or 'down'
           :returns: command output"""
        return intf.cmd( 'ip', 'link', 'set', 'dev', intf.name, *args )

    def link( self, nodeA, nodeB, ipA, ipB ):
        """Add a link the way network_builder/topology.py does.

           :param nodeA: first node
           :param nodeB: second node
           :param ipA: address with prefix length for nodeA's interface
           :param ipB: address with prefix length for nodeB's interface
           :returns: the new link"""
        return self.net.addLink(
            nodeA, nodeB,
            params1={ 'ip': ipA, 'ifconfig': self.ipLinkConf },
            params2={ 'ip': ipB, 'ifconfig': self.ipLinkConf } )

    def setUp( self ):
        """Build and start the network, then install static routes."""
        super().setUp()
        d1 = self.d1 = self.addHost( 'd1', BASH_IPROUTE_IMAGE )
        d2 = self.d2 = self.addHost( 'd2', BASH_IPROUTE_IMAGE )
        wan = self.wan = self.addHost( 'wan', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        er1 = self.er1 = self.addHost( 'er1', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        er2 = self.er2 = self.addHost( 'er2', BUSYBOX_IMAGE, privileged=True,
                                       sysctls=ROUTER_SYSCTLS )
        self.link( d1, er1, '192.168.1.2/24', '192.168.1.1/24' )
        self.link( d2, er2, '192.168.2.2/24', '192.168.2.1/24' )
        self.primary = self.link( er1, wan, '10.0.1.2/30', '10.0.1.1/30' )
        self.link( er1, wan, '20.0.1.2/30', '20.0.1.1/30' )
        self.link( er2, wan, '10.0.2.2/30', '10.0.2.1/30' )
        self.net.start()

        # Each redundant destination gets a primary route (metric 1) over
        # the 10.0.1.0/30 link and a backup route (metric 2) over the
        # 20.0.1.0/30 link
        route = 'ip route replace'
        er1.netCmd( route, '192.168.2.0/24 via 10.0.1.1 metric 1' )
        er1.netCmd( 'ip route add 192.168.2.0/24 via 20.0.1.1 metric 2' )
        er2.netCmd( route, '192.168.1.0/24 via 10.0.2.1' )
        wan.netCmd( route, '192.168.1.0/24 via 10.0.1.2 metric 1' )
        wan.netCmd( 'ip route add 192.168.1.0/24 via 20.0.1.2 metric 2' )
        wan.netCmd( route, '192.168.2.0/24 via 10.0.2.2' )
        d1.setDefaultRoute( 'via 192.168.1.1' )
        d2.setDefaultRoute( 'via 192.168.2.1' )

    def egress( self, node, ip ):
        """Return the interface the kernel would use to reach ip.

           :param node: node to ask
           :param ip: destination address
           :returns: interface name from 'ip route get' (the word after
                     'dev')"""
        return node.netCmd( 'ip route get', ip ).split( ' dev ' )[ 1 ].split()[ 0 ]

    def testPrivilegedRouters( self ):
        """Router containers are privileged and forward IPv4."""
        info = self.er1.dcli.inspect_container( self.er1.did )
        self.assertTrue( info[ 'HostConfig' ][ 'Privileged' ] )
        self.assertEqual( self.er1.cmd( 'cat /proc/sys/net/ipv4/ip_forward' )
                          .strip(), '1' )

    def testEndToEnd( self ):
        """d1 reaches d2, and er1 uses the primary link."""
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )
        self.assertEqual( self.egress( self.er1, '192.168.2.2' ), 'er1-eth1' )

    def testFailoverToBackupLink( self ):
        """With both ends of the primary link down, er1 routes over the
           backup link and d2 stays reachable."""
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )
        self.primary.intf1.set_state( 'down' )
        self.primary.intf2.set_state( 'down' )
        self.assertEqual( self.egress( self.er1, '192.168.2.2' ), 'er1-eth2' )
        self.assertTrue( pingOnce( self.d1, '192.168.2.2' ) )

    def testDegradedPrimaryAddsLatency( self ):
        """40 ms of delay on er1's end of the primary link (one direction
           only) adds about 40 ms to the round trip; clearing the shaping
           restores the original latency."""
        baseline = pingRttMs( self.d1, '192.168.2.2' )
        self.primary.intf1.config( delay='40ms' )
        degraded = pingRttMs( self.d1, '192.168.2.2' )
        self.primary.intf1.config()
        restored = pingRttMs( self.d1, '192.168.2.2' )
        self.assertGreaterEqual( degraded, 38 )
        self.assertLess( restored, baseline + 20 )

    def testInterfaceGraphState( self ):
        """Every interface is up after start; topology.py records these
           states in its topology graph."""
        # topology.py reads the state of every interface after creation
        for host in ( self.d1, self.er1, self.wan ):
            for intf in host.intfList():
                self.assertEqual( intf.get_state(), 'up' )


if __name__ == '__main__':
    unittest.main()
