"""Browser integration: the native-messaging host and its registration.

Nothing in this package may import wx. Browsers start the host once for every
message, so it has to come up quickly, and the frozen app becomes the host
before any GUI code is loaded.
"""
