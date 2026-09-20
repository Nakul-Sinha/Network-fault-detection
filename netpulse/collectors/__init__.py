"""Cross-layer collectors.

Each collector owns one layer of the HMPH feature set, declares whether it is
supported on the current platform, and returns a sparse set of features plus
the probe records describing any traffic it generated.
"""
