"""
compat_support.py: shared helpers for the container compatibility tests.

This fork of Containernet runs Docker nodes built from arbitrary images
(with bash or only a POSIX sh, with or without networking tools). The
test_compat_*.py modules check that this works; this module holds what
they share: the test images, Docker and privilege checks, container
clean-up, ping helpers and the DockerTestCase base class. It contains no
tests itself.

The tests are split by the privileges they need. test_compat_unit.py and
test_compat_simconfig.py test pure logic and need neither Docker nor root.
test_compat_docker.py starts Docker nodes without building a network and
needs only access to the Docker daemon. test_compat_network.py builds full
topologies (veth links, nsenter-based configuration, tc shaping) and needs
root.

Run everything from the containernet directory with::

    sudo python3 -m unittest discover -s mininet/test -p 'test_compat_*.py' -v

Test classes whose prerequisites are missing are skipped, not failed.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from typing import Optional

# Images chosen to cover the three common userland families:
# bash with iproute2, bash without any networking tools, and BusyBox sh
# only. ubuntu:trusty is also the host image of the main project.
BASH_IPROUTE_IMAGE = 'ubuntu:trusty'
BASH_MINIMAL_IMAGE = 'debian:bookworm-slim'
BUSYBOX_IMAGE = 'alpine:3.20'
TEST_IMAGES = ( BASH_IPROUTE_IMAGE, BASH_MINIMAL_IMAGE, BUSYBOX_IMAGE )

# Docker label that Containernet puts on every container it creates
CONTAINER_LABEL = 'com.containernet'


def dockerAvailable() -> bool:
    """Return whether the Docker daemon is reachable by this user.

       :returns: True if 'docker info' succeeds"""
    try:
        return subprocess.run( [ 'docker', 'info' ], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               check=False ).returncode == 0
    except OSError:
        return False


def isRoot() -> bool:
    """Return whether the tests run with root privileges.

       :returns: True if the effective user id is 0"""
    return os.geteuid() == 0


def ensureImages( images=TEST_IMAGES ) -> None:
    """Pull test images that are not present locally.

       :param images: image references
       :raises subprocess.CalledProcessError: if a pull fails"""
    for image in images:
        present = subprocess.run( [ 'docker', 'image', 'inspect', image ],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, check=False )
        if present.returncode != 0:
            subprocess.run( [ 'docker', 'pull', '-q', image ], check=True,
                            stdout=subprocess.DEVNULL )


def containerExists( name: str ) -> bool:
    """Return whether a container with exactly this name exists.

       :param name: container name, e.g. 'mn.h1' (Containernet prefixes
                    node names with 'mn.')
       :returns: True if 'docker inspect' finds the container"""
    return subprocess.run( [ 'docker', 'inspect', '--type', 'container', name ],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False ).returncode == 0


def removeTestContainers() -> None:
    """Force-remove every Containernet container, found by its label.

       Note that this also removes Containernet containers that the tests
       did not create, such as those of a running experiment."""
    ids = subprocess.run( [ 'docker', 'ps', '-aq', '--filter',
                            'label=%s' % CONTAINER_LABEL ],
                          capture_output=True, text=True,
                          check=False ).stdout.split()
    if ids:
        subprocess.run( [ 'docker', 'rm', '-f' ] + ids,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False )


def pingOnce( node, ip: str, timeout: int = 2 ) -> bool:
    """Send one ICMP echo request from a node (BusyBox or iputils ping).

       :param node: Mininet node with a ping binary
       :param ip: destination address
       :param timeout: seconds to wait for the reply
       :returns: True if a reply arrived"""
    out = node.cmd( 'ping -c 1 -W %d %s >/dev/null 2>&1; echo rc=$?'
                    % ( timeout, ip ) )
    return 'rc=0' in out


def pingRttMs( node, ip: str, count: int = 3 ) -> Optional[ float ]:
    """Return the average ping round-trip time.

       :param node: Mininet node with a ping binary
       :param ip: destination address
       :param count: number of echo requests
       :returns: average RTT in milliseconds, or None if ping printed no
                 summary (for example because every request was lost)"""
    out = node.cmd( 'ping -c %d -W 2 %s' % ( count, ip ) )
    # The summary line is 'rtt min/avg/max/mdev = 0.04/0.06/0.07/0.01 ms'
    # (iputils) or 'round-trip min/avg/max = 0.1/0.2/0.3 ms' (BusyBox);
    # the group captures the second number, the average
    match = re.search( r'= [\d.]+/([\d.]+)/', out )
    return float( match.group( 1 ) ) if match else None


class DockerTestCase( unittest.TestCase ):
    """Base class for tests that start containers.

       Skips the whole class when Docker (or root, if needsRoot is set) is
       not available, pulls the test images, and removes Containernet
       containers before the class and after every test."""

    #: Set to True in subclasses whose tests need root (veth links,
    #: nsenter)
    needsRoot = False

    @classmethod
    def setUpClass( cls ) -> None:
        """Skip the class if prerequisites are missing; pull images.

           :raises unittest.SkipTest: without Docker, or without root when
                                      needsRoot is set"""
        if not dockerAvailable():
            raise unittest.SkipTest( 'Docker daemon not reachable' )
        if cls.needsRoot and not isRoot():
            raise unittest.SkipTest( 'requires root (run with sudo)' )
        ensureImages()
        removeTestContainers()

    def tearDown( self ) -> None:
        """Remove all Containernet containers so none leak between
           tests."""
        removeTestContainers()
