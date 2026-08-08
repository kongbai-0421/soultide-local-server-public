# Android Local Service Source

This directory contains only the original Java source and Android manifest for
the companion local-service application. It starts a loopback-only HTTP
listener and coordinates a user-supplied local runtime.

It intentionally excludes APKs, signing keys, game packages, game resources,
account files, captured traffic, and embedded runtime payloads. A release
build therefore requires the developer to provide their own Android SDK and
runtime inputs in a private build environment.

The Java listener is bound only to `127.0.0.1`; it is not an Internet service
and must not be exposed to a network.
