"""One reconcile rule for both listener transports (issue #107, ADR-0004).

The Telnet and SSH listener adapters applied their config section with two copies of the same naming
rule. Both copies had the same defect (issue #107): an entry the adapter could not build was still
reported under ``added``, so a reload answer claimed a listener that never listened, and nothing said
why. This module holds the rule once: which names were added, updated, unchanged, removed, or
refused, and what the operator must be told about each.

What stays per transport, in the host adapter: ``_build_listener`` (builds one spec or raises),
``_start_single_listener`` (binds one spec, returns whether it came up), ``_stop_single_listener``
(stops one by name), and ``_prepare_listener_start`` when the transport needs a shared secret first.

The contract it implements is the one the port adapters keep (issue #105), so one reader covers both:

* a name that did not become a running listener never appears under ``added`` or ``updated``;
* it appears under ``failed`` with its reason in ``failed_detail``, from
  ``BaseGenericAdapter.reconcile_summary()``;
* a refused change to a running listener keeps that listener and says so under ``kept_previous``;
* every refusal is recorded with ``record_config_gap()``, so the boot block, the reload block and
  ``adapters.<kind>.failed`` name it, and an entry the file stops listing ends its gap.

A listener that builds but cannot bind is refused the same way, and its spec is dropped from
``listeners``. That is what lets a later reload retry it: the file's entry is unchanged, so keeping
the dead spec would classify it as ``unchanged`` and never bind it again, leaving the gap open after
the operator freed the port.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..listener_session import ListenerConfig

#: The gap reason for a spec that built cleanly but whose socket would not bind. The transport's own
#: ERROR line carries the operating system's words; this one names the listener in the gap report.
LISTENER_UNBOUND = "the listener socket could not be bound (is the port already in use?)"


def listener_section_items(section: str, new_config: Any) -> List[Tuple[str, Any]]:
    """Label every entry of a reconcile payload, in file order.

    Accepts the wrapped ``{"<section>": [...]}`` form or a bare list; anything else is an empty
    section. An entry with no usable name is kept and labelled by its position, because a silently
    skipped entry is the defect this module fixes. ``BaseGenericAdapter.prune_config_gaps()`` uses the
    same ``<unnamed #N>`` label, so a gap recorded here is a gap it can later drop.

    Args:
        section: The adapter's config key, for example ``telnet_listener``.
        new_config: The payload ``reconcile_ports()`` received.

    Returns:
        ``(label, entry)`` pairs, where ``entry`` may be anything the file held.
    """
    if isinstance(new_config, dict) and isinstance(new_config.get(section), list):
        items = list(new_config[section])
    elif isinstance(new_config, list):
        items = list(new_config)
    else:
        items = []
    return [(entry_label(entry, index), entry) for index, entry in enumerate(items)]


def entry_label(entry: Any, index: int) -> str:
    """Name one listener entry the way the gap report names it.

    Args:
        entry: The raw entry from the config file.
        index: Its position in the section, counted from zero.
    """
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return f"<unnamed #{index + 1}>"


def listener_material(spec: ListenerConfig) -> Dict[str, Any]:
    """The restart-triggering fields of a listener spec, for an old/new comparison."""
    return {
        "bind_host": spec.bind_host,
        "bind_port": spec.bind_port,
        "target": spec.target,
        "read_only": spec.read_only,
        "acl_raw": list(spec.acl_raw),
        "enabled": spec.enabled,
        "require_auth": spec.require_auth,
    }


def _reason(error: BaseException) -> str:
    """The operator-facing reason carried by a build failure."""
    return str(error).strip() or "the entry could not be read"


class ListenerReconcileMixin:
    """Apply a listener section to one transport's running listeners.

    The host adapter must provide ``listener_section``, ``listener_display``, ``listeners``,
    ``logger``, ``_build_listener``, ``_start_single_listener`` and ``_stop_single_listener``, plus the
    config-gap API from ``BaseGenericAdapter``. Everything here is shared by both transports.
    """

    #: Config key holding this adapter's listener entries, eg ``telnet_listener``.
    listener_section: str = ""
    #: Word used in this transport's log lines, eg ``Telnet``.
    listener_display: str = ""

    # ------------------------------------------------------------------
    # Boot

    def prepare_port(self, port_name: str, config: Dict[str, Any]) -> ListenerConfig:
        """Pre-validate one listener entry, so one bad entry costs only itself.

        ``BaseGenericAdapter.reject_unusable_entries()`` calls this when the config section fails
        ``validate_config()``. Without it the whole section is skipped and every usable listener of
        that section is lost with the refused one (issue #107).

        Args:
            port_name: The name the file gave the listener; unused, the entry carries it.
            config: The raw listener entry.

        Raises:
            ValueError: The entry cannot become a listener, with the reason for the gap report.
        """
        return self._build_listener(config)

    def build_listener_entries(self, entries: Any) -> List[ListenerConfig]:
        """Build the usable specs of a boot section and record a gap for each refused entry.

        Args:
            entries: The raw list the adapter's config section holds.

        Returns:
            One spec per usable entry, in file order.
        """
        specs: List[ListenerConfig] = []
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            label = entry_label(entry, index)
            try:
                specs.append(self._build_listener(entry))
            except Exception as exc:
                self._refuse(label, _reason(exc))
        return specs

    async def start_listeners(self, specs: List[ListenerConfig]) -> bool:
        """Bind the given specs and keep only the listeners that came up.

        Args:
            specs: The specs built from the adapter's config section at boot.

        Returns:
            True when every one of them is listening.
        """
        ready = await self._start_specs(specs, refused={})
        self.listeners = ready
        return len([spec for spec in specs if spec.enabled]) == len([spec for spec in ready if spec.enabled])

    async def _do_start(self) -> bool:
        """Bind every listener of this adapter, recording a gap for each one that stays down."""
        if not self.listeners:
            self.logger.info("%s listener adapter has no entries; nothing to bind", self.listener_display)
            return True
        if not self.console_manager:
            self.logger.error("%s listener adapter requires a console manager reference", self.listener_display)
            return False
        return await self.start_listeners(self.listeners)

    # ------------------------------------------------------------------
    # Reload

    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Bring the running listeners to one new config section, without disturbing other sessions.

        Every entry is built before anything is stopped, so an entry the adapter refuses never costs a
        running listener -- the same prepare-before-destroy order the port adapters keep (issue #105).

        Args:
            new_config: The wrapped ``{"<section>": [...]}`` form or a bare list of entries.

        Returns:
            ``added``, ``removed``, ``updated``, ``unchanged``, and -- when something was refused --
            ``failed``, ``failed_detail`` and ``kept_previous``.
        """
        items = listener_section_items(self.listener_section, new_config)
        # An entry the file still lists keeps its gap; one it deleted loses its claim on the report.
        self.prune_config_gaps([entry for _label, entry in items])

        refused: Dict[str, str] = {}
        specs_by_label: Dict[str, ListenerConfig] = {}
        for label, entry in items:
            try:
                specs_by_label[label] = self._build_listener(entry)
            except Exception as exc:
                refused[label] = _reason(exc)
                self._refuse(label, refused[label])

        old_by_name = {spec.name: spec for spec in self.listeners}
        wanted = set(specs_by_label) | set(refused)
        removed = sorted(set(old_by_name) - wanted)
        added = sorted(wanted - set(old_by_name))
        updated: List[str] = []
        unchanged: List[str] = []
        kept_previous: List[str] = []
        for name in sorted(set(old_by_name) & wanted):
            if name in refused:
                # The running listener serves the previous config: it keeps running, appears in no name
                # list, and is reported under kept_previous -- as a refused port is (issue #105). The
                # refusal was already logged and recorded while the entries were built, so only the
                # "kept" fact is added here.
                kept_previous.append(name)
                self.record_config_gap(name, refused[name], kept_previous=True)
                continue
            if listener_material(old_by_name[name]) == listener_material(specs_by_label[name]):
                unchanged.append(name)
                self.clear_config_gap(name)
            else:
                updated.append(name)

        for name in removed + updated:
            await self._stop_single_listener(name)

        # `added` still names the refused adds, so that the summary can strip them and report them as
        # failed; only an entry that built has a spec to bind.
        starting = [specs_by_label[name] for name in added + updated if name in specs_by_label]
        ready = await self._start_specs(starting, refused=refused)
        by_name = {spec.name: spec for spec in self.listeners if spec.name not in removed + updated}
        for spec in ready:
            by_name[spec.name] = spec
        self.listeners = [by_name[name] for name in sorted(by_name)]
        self._set_running(True)

        summary = self.reconcile_summary(added, removed, updated, unchanged, refused=refused, kept_previous=kept_previous)
        self.logger.info(
            "%s listener adapter %s reconcile: +%s ~%s -%s unchanged=%s failed=%s",
            self.listener_display,
            self.name,
            len(summary["added"]),
            len(summary["updated"]),
            len(removed),
            len(unchanged),
            len(refused),
        )
        return summary

    # ------------------------------------------------------------------
    # Shared internals

    async def _start_specs(
        self,
        specs: List[ListenerConfig],
        refused: Dict[str, str],
    ) -> List[ListenerConfig]:
        """Bind every enabled spec and record a refusal for each one that stays down.

        Args:
            specs: The specs to bring up, in order.
            refused: Refusals of this pass, updated in place with any bind failure. A bind failure is
                never a kept_previous case: the stale listener was already stopped, so nothing serves
                that entry any more.

        Returns:
            The specs that are listening, plus every disabled one, which is intentionally not bound. A
            refused spec is never returned: the caller keeps what this returns, and a spec kept for one
            that never bound is read as ``unchanged`` by the next reload and is then never bound again.
        """
        if not [spec for spec in specs if spec.enabled]:
            return list(specs)
        blocked = await self._prepare_listener_start()
        if blocked:
            for spec in specs:
                if spec.enabled:
                    self._refuse(spec.name, blocked)
                    refused[spec.name] = blocked
            # Nothing bound, so only the disabled specs are kept: a start with enabled listeners left
            # unbound is a failed start, not a running adapter with invisible listeners.
            return [spec for spec in specs if not spec.enabled]

        ready: List[ListenerConfig] = []
        for spec in specs:
            if not spec.enabled:
                self.logger.info("%s listener '%s' disabled via configuration", self.listener_display, spec.name)
                self.clear_config_gap(spec.name)
                ready.append(spec)
                continue
            if await self._start_single_listener(spec):
                self.clear_config_gap(spec.name)
                ready.append(spec)
                continue
            self._refuse(spec.name, LISTENER_UNBOUND)
            refused[spec.name] = LISTENER_UNBOUND
            await self._stop_single_listener(spec.name)
        return ready

    def _refuse(self, label: str, reason: str, kept_previous: bool = False) -> None:
        """Log and record one listener entry that did not become a running listener."""
        self.logger.error("%s adapter %s rejects listener %s: %s", self.listener_display, self.name, label, reason)
        self.record_config_gap(label, reason, kept_previous=kept_previous)

    async def _prepare_listener_start(self) -> Optional[str]:
        """Prepare anything every listener of this transport needs before the first bind.

        Returns:
            None when the transport is ready, else the reason none of its listeners can bind.
        """
        return None
