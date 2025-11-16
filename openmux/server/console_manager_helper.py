"""
Helper module to provide direct access to the console manager for loopback ports
"""

import logging

# Global reference to the console manager instance
_console_manager = None


def set_console_manager(console_manager):
    """
    Set the global console manager instance
    """
    global _console_manager
    _console_manager = console_manager
    logging.getLogger("openmux.helper").info("Console manager reference set")


# Removed deprecated direct_send_to_port_clients helper (unused)
