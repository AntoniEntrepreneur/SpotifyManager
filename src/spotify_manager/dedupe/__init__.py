"""Duplicate-edition analysis of the saved-album library.

Everything in this package is pure: it takes a library snapshot as data and returns
a plan as data. Nothing here touches the network, the filesystem or the clock, and
nothing here imports from `infra`.
"""
