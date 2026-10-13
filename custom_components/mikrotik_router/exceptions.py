"""Exceptions for Mikrotik Router."""


class ApiEntryNotFound(Exception):
    """Api entry not found."""


class ApiReadOnly(Exception):
    """A write to the router was refused because the entry is read-only."""
