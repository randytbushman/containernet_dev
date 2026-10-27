"""
container.py: helpers that make Containernet work with arbitrary images.

Containernet runs each Docker node through an interactive "control shell"
started with ``docker exec`` inside the container. Mininet writes commands
to that shell and knows a command has finished when the shell prints its
prompt, which Containernet sets to a sentinel character (chr( 127 )).
Different images ship different shells, and images are named by references
that are easy to parse wrongly; this module handles both.

The module contains:

- ImageReference, which parses Docker image references of the form
  ``[registry[:port]/]repository[:tag][@digest]`` correctly, including
  registries with ports.
- ShellProfile and its subclasses BashShellProfile and PosixShellProfile,
  which describe how to start and drive the control shell. Profiles are
  registered in priority order and detected per container, so images that
  ship only a POSIX sh (Alpine/BusyBox ash, Debian dash) work as well as
  bash images. The Docker node class in mininet/node.py uses them.
- PID_MARKER_CMD and INTERACTIVE_SHELLS, constants shared with node.py.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional


# Appended to a backgrounded command: prints ^A<pid>\n of the new job.
# $! is the PID of the last background job; \001 is ^A (chr( 1 )) and
# \012 is a newline. Node.monitor() looks for ^A followed by digits to set
# Node.lastPid, as it does for the output of 'mnexec -p' on classic hosts.
PID_MARKER_CMD = ' printf "\\001%d\\012" $! '

# Shells that an image's CMD commonly names just to provide a prompt.
# Containernet already runs its own shell, so these are never re-launched
# (see Docker.get_cmd_field() in node.py).
INTERACTIVE_SHELLS = frozenset( [ 'bash', '/bin/bash', '/usr/bin/bash',
                                  'sh', '/bin/sh', '/usr/bin/sh',
                                  'ash', '/bin/ash' ] )


@dataclass( frozen=True )
class ImageReference( object ):
    """A parsed Docker image reference, such as 'ubuntu:trusty' or
       'registry:5000/team/img@sha256:...'.

       Instances are immutable and compare equal when all parts match."""

    #: Repository including any registry host and port, e.g.
    #: 'localhost:5000/img'
    repository: str
    #: Tag after the last ':' of the name, or None if there is none
    tag: Optional[ str ] = None
    #: Content digest after '@', e.g. 'sha256:<64 hex digits>', or None
    digest: Optional[ str ] = None

    @classmethod
    def parse( cls, reference: str ) -> ImageReference:
        """Parse an image reference string.

           A ':' only denotes a tag if it follows the last '/', so
           'localhost:5000/img' is a repository without a tag.

           :param reference: e.g. 'ubuntu:trusty' or
                             'reg:5000/team/img@sha256:...'
           :returns: the parsed ImageReference"""
        name, _, digest = reference.partition( '@' )
        lastSlash = name.rfind( '/' )
        lastColon = name.rfind( ':' )
        if lastColon > lastSlash:
            return cls( name[ :lastColon ], name[ lastColon + 1: ] or None,
                        digest or None )
        return cls( name, None, digest or None )

    @property
    def pullTag( self ) -> str:
        """Tag or digest to request from the registry ('latest' if unset).

           Never empty: an empty tag makes the Docker daemon pull every
           tag of the repository."""
        return self.digest or self.tag or 'latest'

    def __str__( self ) -> str:
        """Return the canonical reference string.

           :returns: 'repository@digest' if there is a digest, otherwise
                     'repository:tag' with the tag defaulting to 'latest'"""
        if self.digest:
            return '%s@%s' % ( self.repository, self.digest )
        return '%s:%s' % ( self.repository, self.tag or 'latest' )


class ShellProfile( ABC ):
    """How to start and drive a sentinel-prompt control shell inside a
       container.

       Each subclass describes one shell (its argv, environment and how it
       starts background jobs). Instances are registered with
       ShellProfile.register() in priority order; detect() picks the first
       one available in a container, unless the node forces one by name
       (the Docker node's dshell parameter)."""

    #: Shell executable name inside the container
    executable: ClassVar[ str ] = ''
    #: Registered profiles by executable name; dict order is the priority
    #: order used by detect()
    _registry: ClassVar[ dict[ str, ShellProfile ] ] = {}

    @abstractmethod
    def argv( self, label: str ) -> list[ str ]:
        """Return the argv that starts an interactive shell reading stdin.

           :param label: marker placed in the process' argv (e.g.
                         'mininet:h1') so stale shells can be found and
                         killed by 'mn -c'
           :returns: argument list to run with 'docker exec'"""

    def environment( self, prompt: str ) -> dict[ str, str ]:
        """Return environment variables passed via 'docker exec -e'.

           :param prompt: sentinel prompt string Mininet waits for
           :returns: variable name to value; at least PS1, the prompt"""
        return { 'PS1': prompt }

    def backgroundCommand( self, command: str ) -> str:
        """Return shell text that runs a background command and reports its
           PID.

           The text runs the command and then prints ^A<pid>\\n, the marker
           Node.monitor() uses to set lastPid.

           :param command: command line ending in '&'
           :returns: the shell text to send"""
        return command + PID_MARKER_CMD

    def isAvailableIn( self, container: str ) -> bool:
        """Return whether this shell can run inside a container.

           Probes by running ``SHELL -c true`` with 'docker exec'.

           :param container: container name or id
           :returns: True if the probe exited successfully within 30
                     seconds"""
        try:
            probe = subprocess.run(
                [ 'docker', 'exec', container, self.executable, '-c', 'true' ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=30 )
        except ( OSError, subprocess.TimeoutExpired ):
            return False
        return probe.returncode == 0

    @classmethod
    def register( cls, profile: ShellProfile ) -> None:
        """Register a profile; earlier registrations have priority.

           :param profile: ShellProfile instance, keyed by its executable"""
        cls._registry[ profile.executable ] = profile

    @classmethod
    def forName( cls, name: str ) -> ShellProfile:
        """Return the profile registered for a shell name.

           :param name: shell executable name, e.g. 'bash' or 'sh'
           :returns: the registered ShellProfile
           :raises ValueError: if no profile is registered for the name"""
        try:
            return cls._registry[ name ]
        except KeyError:
            raise ValueError( 'Unknown shell %r; registered shells: %s'
                              % ( name, ', '.join( cls._registry ) ) )

    @classmethod
    def detect( cls, container: str,
                preferred: Optional[ str ] = None ) -> ShellProfile:
        """Pick the shell to use for a container.

           :param container: container name or id
           :param preferred: force a registered shell instead of probing
           :returns: the preferred profile, or else the first registered
                     profile whose shell exists in the container
           :raises ValueError: if preferred names an unknown shell
           :raises RuntimeError: if the image provides no supported
                                 shell"""
        if preferred:
            return cls.forName( preferred )
        for profile in cls._registry.values():
            if profile.isAvailableIn( container ):
                return profile
        raise RuntimeError(
            'Container %s provides none of the supported shells (%s). '
            'Containernet needs a shell inside the image to run commands.'
            % ( container, ', '.join( cls._registry ) ) )


class BashShellProfile( ShellProfile ):
    """GNU bash.

       Start-up files are skipped (--norc) and line editing is disabled
       (--noediting) so readline never emits terminal control sequences
       (e.g. bracketed paste) into command output."""

    #: Shell executable name inside the container
    executable = 'bash'

    def argv( self, label: str ) -> list[ str ]:
        """Return the bash command line; see ShellProfile.argv.

           ``-i`` makes the shell interactive (it prints the prompt) and
           ``-s`` makes it read commands from stdin, with the label as its
           only positional argument.

           :param label: marker placed in the process' argv
           :returns: argument list to run with 'docker exec'"""
        return [ 'bash', '--norc', '--noediting', '-is', label ]


class PosixShellProfile( ShellProfile ):
    """Any POSIX sh (dash, BusyBox ash, and so on), used for images
       without bash."""

    def backgroundCommand( self, command: str ) -> str:
        """Start the job from a subshell that exits immediately.

           BusyBox ash re-prints its prompt (our sentinel) when a job ends
           while it waits for input, which would cut the output of the
           next command short. Detached jobs never notify the control
           shell; the trade-off is that %job references and 'wait' do not
           apply to them (use the PID from lastPid instead).

           :param command: command line ending in '&'
           :returns: the shell text to send"""
        return '( %s )' % ( command + PID_MARKER_CMD )

    #: Shell executable name inside the container
    executable = 'sh'

    def argv( self, label: str ) -> list[ str ]:
        """Return the sh command line; see ShellProfile.argv.

           :param label: marker placed in the process' argv
           :returns: argument list to run with 'docker exec'"""
        return [ 'sh', '-is', label ]

    def environment( self, prompt: str ) -> dict[ str, str ]:
        """Return the environment; see ShellProfile.environment.

           Also sets ENV to an empty string: an interactive POSIX shell
           runs the start-up file named by $ENV, which could print output
           or change the prompt.

           :param prompt: sentinel prompt string Mininet waits for
           :returns: variable name to value"""
        env = super().environment( prompt )
        env[ 'ENV' ] = ''
        return env


# Priority order: bash when the image has it, otherwise any POSIX sh
ShellProfile.register( BashShellProfile() )
ShellProfile.register( PosixShellProfile() )
