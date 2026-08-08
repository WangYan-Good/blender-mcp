# Blender MCP Core — Terms of Use and Privacy Notice

**Last updated: August 8, 2026**

## Open-source software

Blender MCP Core is provided under the repository's MIT License. The software is
provided "as is", without warranty of any kind. To the maximum extent permitted
by applicable law, its contributors are not liable for claims or damages arising
from use of the software.

## Core privacy behavior

This core build does not implement telemetry or remote upload of prompts, Python
source, scene metadata, Blender files, renders, or viewport images. The Blender
add-on and MCP bridge communicate locally by default. Generated `.blend` and PNG
files remain in the configured workspace unless another program or a script
moves or transmits them.

The project contributors receive no license to a user's prompts, source code,
Blender scenes, renders, or other creative work merely because the user runs
this software. Users retain whatever rights they otherwise hold in that content.

Local operational logs may record command type, request ID, byte count, timing,
and connection state. They do not intentionally record the body of submitted
Python source.

## Unrestricted Python and local access

`execute_python` intentionally runs arbitrary code with the permissions of the
Blender process. Such code can access the filesystem and network, start other
processes, alter or delete data, or transmit information. Those actions are
caused by code the user or the user's agent chooses to execute; they are not
telemetry performed by the core project. Users are responsible for reviewing and
authorizing code and for maintaining backups.

The Blender socket binds to localhost and has no application-level
authentication. Other local processes that can connect to the configured port
may invoke the same capabilities. Users must not expose or forward that port to
untrusted networks.

Managed-path validation is a correctness boundary, not an operating-system
sandbox against malicious same-user processes that race filesystem reparse
points. The core should be run only alongside trusted local software.

## Third-party clients and services

An MCP client, AI provider, operating system service, package registry, or code
executed through Blender may process data under separate terms and privacy
policies. This project does not control those third parties. Users should review
their client/provider configuration before sending sensitive scenes, text, code,
or images.

## Content and acceptable use

Users are responsible for having the rights and permissions needed for content
they process and for complying with applicable law. The software must not be
used to access systems or data without authorization.

## Changes

These terms may change with future releases. The terms shipped with the version
being used apply to that version.

Blender MCP Core is an independent project and is not affiliated with or
endorsed by the Blender Foundation.
