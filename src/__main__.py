import argparse
from src.domains import BlockDomainConverter, AllowDomainConverter
from src import utils, info, silent_error, error, BLOCK_PREFIX, ALLOW_PREFIX, ENABLE_SNI_FILTER
from src.cloudflare import (
    create_list, update_list, create_rule,
    update_rule, delete_list, delete_rule, get_rules
)
from src.requests import NotFoundException

# Cloudflare Gateway free tier hard limit for number of lists
MAX_TOTAL_LISTS = 300


class CloudflareManager:
    def __init__(self, cache):
        self.cache = cache

        # Block config
        self.block_list_name = f"[{BLOCK_PREFIX}]"
        self.block_rule_name = f"[{BLOCK_PREFIX}] Block Ads"
        self.block_sni_rule_name = f"[{BLOCK_PREFIX}] Block Ads (SNI)"

        # Allow config
        self.allow_list_name = f"[{ALLOW_PREFIX}]"
        self.allow_rule_name = f"[{ALLOW_PREFIX}] Allow"

    def _resolve_rule_priorities(self):
        """Resolve collision-free Gateway rule precedence values.

        Preserve the existing managed ordering when all required rules are
        already present and valid. If one is missing (or the ordering is
        invalid), allocate a fresh consecutive range after every existing
        Gateway rule so creation/update cannot collide with unrelated rules.
        """
        current_rules = get_rules("")

        managed_names = {
            self.allow_rule_name,
            self.block_rule_name,
        }
        if ENABLE_SNI_FILTER:
            managed_names.add(self.block_sni_rule_name)

        managed = {
            rule["name"]: rule
            for rule in current_rules
            if rule.get("name") in managed_names
        }

        allow_rule = managed.get(self.allow_rule_name)
        block_rule = managed.get(self.block_rule_name)
        sni_rule = managed.get(self.block_sni_rule_name) if ENABLE_SNI_FILTER else None

        required_rules = [allow_rule, block_rule]
        if ENABLE_SNI_FILTER:
            required_rules.append(sni_rule)

        if all(required_rules):
            managed_precedences = [rule.get("precedence") for rule in required_rules]
            if (
                all(isinstance(value, int) for value in managed_precedences)
                and len(set(managed_precedences)) == len(managed_precedences)
                and allow_rule["precedence"] < block_rule["precedence"]
            ):
                return (
                    block_rule["precedence"],
                    allow_rule["precedence"],
                    sni_rule["precedence"] if sni_rule else None,
                )

        used_precedences = [
            rule["precedence"]
            for rule in current_rules
            if isinstance(rule.get("precedence"), int)
        ]
        first_free = max(1000, max(used_precedences, default=999) + 1)

        allow_priority = first_free
        block_priority = first_free + 1
        sni_priority = first_free + 2 if ENABLE_SNI_FILTER else None

        return block_priority, allow_priority, sni_priority

    # ------------------------------------------------------------------
    # Generic list/rule sync (shared by both block and allow)
    # ------------------------------------------------------------------
    def _sync_rule(self, list_ids, rule_name, rule_action, rule_priority,
                    filters=None, traffic_field="dns.domains"):
        current_rules = utils.get_current_rules(self.cache, rule_name)
        existing_rule = next((r for r in current_rules if r["name"] == rule_name), None)
        existing_list_ids = utils.extract_list_ids(existing_rule)

        if existing_rule:
            precedence_changed = existing_rule.get("precedence") != rule_priority
            if set(list_ids) != existing_list_ids or precedence_changed:
                try:
                    updated = update_rule(rule_name, existing_rule["id"], list_ids,
                                          action=rule_action, priority=rule_priority,
                                          filters=filters, traffic_field=traffic_field)
                    info(f"[~] Updated rule: {updated['name']}")
                    self.cache["rules"] = [
                        r for r in self.cache["rules"] if r["id"] != existing_rule["id"]
                    ]
                    self.cache["rules"].append(updated)
                except NotFoundException:
                    silent_error(
                        f"[·] Rule {rule_name} ({existing_rule['id']}) missing on Cloudflare "
                        f"— evicting from cache and recreating"
                    )
                    self.cache["rules"] = [
                        r for r in self.cache["rules"] if r["id"] != existing_rule["id"]
                    ]
                    rule = create_rule(rule_name, list_ids, action=rule_action,
                                       priority=rule_priority, filters=filters,
                                       traffic_field=traffic_field)
                    info(f"[+] Recreated rule: {rule['name']}")
                    self.cache["rules"].append(rule)
            else:
                silent_error(f"[·] Skipping rule update (unchanged): {rule_name}")
        else:
            rule = create_rule(rule_name, list_ids, action=rule_action,
                               priority=rule_priority, filters=filters,
                               traffic_field=traffic_field)
            info(f"[+] Created rule: {rule['name']}")
            self.cache["rules"].append(rule)

        utils.save_cache(self.cache)

    def _delete_rule_by_name(self, rule_name):
        current_rules = utils.get_current_rules(self.cache, rule_name)
        for rule in current_rules:
            try:
                delete_rule(rule["id"])
                info(f"[−] Deleted rule: {rule['name']}")
            except NotFoundException:
                silent_error(f"[·] Rule {rule['name']} already gone on Cloudflare — skipping")
            self.cache["rules"] = [r for r in self.cache["rules"] if r["id"] != rule["id"]]
            utils.save_cache(self.cache)

    def _sync_lists(self, domains, list_name_prefix, rule_name, rule_action, rule_priority,
                     sni_rule_name=None, sni_rule_priority=None):
        # --- Fast path: skip entire sync when domain set is unchanged ---
        cached_hash, cached_domains = utils.get_cached_domain_state(self.cache, list_name_prefix)
        current_hash = utils.compute_domain_hash(domains)
        if cached_hash is not None and cached_hash == current_hash:
            info(f"[·] No changes detected for {list_name_prefix} ({len(domains)} domains) — skipping all lists")
            current_lists = utils.get_current_lists(self.cache, list_name_prefix)
            return [lst["id"] for lst in current_lists]

        # --- Domains changed: compute diff ---
        to_add, to_remove = utils.get_domain_diff(domains, cached_domains)
        if cached_hash is None:
            info(f"[+] First sync for {list_name_prefix} — creating {len(domains)} domains")
        else:
            info(f"[⟳] {list_name_prefix} changed: +{len(to_add)} / -{len(to_remove)} domains")

        utils.set_cached_domain_state(self.cache, list_name_prefix, domains)
        utils.save_cache(self.cache)

        current_lists = utils.get_current_lists(self.cache, list_name_prefix)
        list_name_to_id = {lst["name"]: lst["id"] for lst in current_lists}

        # --- Choose sync strategy ---
        rev_map = utils.get_cached_reverse_mapping(self.cache, list_name_prefix)
        if rev_map:
            # Surgical: only touch lists affected by to_remove / to_add
            info(f"[↓] Using reverse mapping ({len(rev_map)} entries) for surgical sync")
            new_list_ids = self._sync_lists_surgical(
                domains, current_lists, to_add, to_remove, rev_map,
                list_name_prefix,
            )
        else:
            # Fallback: first run — load items from every list
            info(f"[⟳] No reverse mapping yet — falling back to full per-list sync")
            new_list_ids = self._sync_lists_full(
                domains, current_lists, list_name_to_id, list_name_prefix,
            )

        # Rebuild reverse mapping from updated forward mapping
        new_rev = utils.build_reverse_mapping(self.cache, new_list_ids, list_name_prefix)
        utils.set_cached_reverse_mapping(self.cache, list_name_prefix, new_rev)

        # Sync DNS rule
        self._sync_rule(new_list_ids, rule_name, rule_action, rule_priority)

        # Optional SNI (L4) rule
        if sni_rule_name:
            self._sync_rule(
                new_list_ids, sni_rule_name, rule_action,
                sni_rule_priority if sni_rule_priority is not None else rule_priority,
                filters=["l4"], traffic_field="net.sni.domains",
            )

        utils.save_cache(self.cache)
        return len(new_list_ids)

    # ------------------------------------------------------------------
    # Surgical sync: uses reverse mapping to only touch affected lists
    # ------------------------------------------------------------------
    def _sync_lists_surgical(self, domains, current_lists, to_add, to_remove,
                              rev_map, list_name_prefix):
        new_domain_set = set(domains)

        # Build per-list remove sets via reverse mapping
        list_removes = {}
        for domain in to_remove:
            lid = rev_map.get(domain)
            if lid is not None:
                list_removes.setdefault(lid, set()).add(domain)

        remaining_add = list(to_add)
        new_list_ids = []

        for lst in current_lists:
            lid = lst["id"]
            removes = list_removes.get(lid, set())

            # Load this list's current items from forward mapping (cache)
            current_values = set(self.cache.get("mapping", {}).get(lid, []))
            chunk = current_values - removes

            if not chunk:
                try:
                    delete_list(lid)
                    info(f"[−] Deleted list: {lst['name']} (no longer needed)")
                except NotFoundException:
                    silent_error(f"[·] List {lst['name']} already gone on Cloudflare — skipping")
                self.cache["lists"] = [l for l in self.cache["lists"] if l["id"] != lid]
                self.cache["mapping"].pop(lid, None)
                utils.save_cache(self.cache)
                continue

            # Fill freed space (and any existing room) with new domains
            new_items = []
            if len(chunk) < 1000 and remaining_add:
                needed_items = 1000 - len(chunk)
                new_items = remaining_add[:needed_items]
                remaining_add = remaining_add[needed_items:]
                chunk.update(new_items)

            if removes or new_items:
                try:
                    update_list(lid, removes, set(new_items))
                    info(
                        f"[~] Updated list: {lst['name']} "
                        f"| Added {len(new_items)}, Removed {len(removes)} "
                        f"| Total: {len(chunk)}"
                    )
                    self.cache["mapping"][lid] = sorted(chunk)
                except NotFoundException:
                    silent_error(
                        f"[·] List {lst['name']} ({lid}) missing on Cloudflare "
                        f"— evicting from cache and recreating"
                    )
                    self.cache["lists"] = [l for l in self.cache["lists"] if l["id"] != lid]
                    self.cache["mapping"].pop(lid, None)
                    lst_created = create_list(lst["name"], sorted(chunk))
                    info(f"[+] Recreated list: {lst_created['name']} with {len(chunk)} domains")
                    self.cache["lists"].append(lst_created)
                    self.cache["mapping"][lst_created["id"]] = sorted(chunk)
                    lid = lst_created["id"]
                utils.save_cache(self.cache)
            else:
                silent_error(f"[·] Skipped (no changes): {lst['name']} | Total: {len(chunk)}")

            new_list_ids.append(lid)

        # Fill remaining new domains into lists with space
        for lid in list(new_list_ids):
            if not remaining_add:
                break
            current_values = set(self.cache.get("mapping", {}).get(lid, []))
            if len(current_values) >= 1000:
                continue
            needed_items = 1000 - len(current_values)
            new_items = remaining_add[:needed_items]
            remaining_add = remaining_add[needed_items:]
            current_values.update(new_items)
            try:
                update_list(lid, set(), set(new_items))
                info(f"[+] Filled list: {len(new_items)} domains | Total: {len(current_values)}")
                self.cache["mapping"][lid] = sorted(current_values)
            except NotFoundException:
                silent_error(f"[·] List {lid} missing — skipping fill")
            utils.save_cache(self.cache)

        # Create new lists for leftover domains
        existing_indexes = []
        for lst in current_lists:
            try:
                existing_indexes.append(int(lst["name"].split("-")[-1]))
            except (ValueError, IndexError):
                pass
        next_index = max(existing_indexes + [0]) + 1

        while remaining_add:
            needed_items = min(1000, len(remaining_add))
            new_items = remaining_add[:needed_items]
            remaining_add = remaining_add[needed_items:]
            list_name = f"{list_name_prefix} - {next_index:03d}"
            lst = create_list(list_name, new_items)
            info(f"[+] Created list: {lst['name']} with {len(new_items)} domains")
            self.cache["lists"].append(lst)
            self.cache["mapping"][lst["id"]] = new_items
            utils.save_cache(self.cache)
            new_list_ids.append(lst["id"])
            next_index += 1

        return new_list_ids

    # ------------------------------------------------------------------
    # Full sync: loads items from every list (fallback for first run)
    # ------------------------------------------------------------------
    def _sync_lists_full(self, domains, current_lists, list_name_to_id, list_name_prefix):
        list_id_to_domains = {}
        for lst in current_lists:
            items = utils.get_list_items_cached(self.cache, lst["id"])
            list_id_to_domains[lst["id"]] = set(items)

        domain_to_list_id = {
            domain: lid
            for lid, doms in list_id_to_domains.items()
            for domain in doms
        }

        remaining_domains = set(domains) - set(domain_to_list_id.keys())
        existing_indexes = sorted(
            [int(name.split("-")[-1]) for name in list_name_to_id.keys()]
        )
        needed_lists = (len(domains) + 999) // 1000
        all_indexes = set(range(1, max(existing_indexes + [needed_lists]) + 1))

        new_list_ids = []
        for i in sorted(all_indexes):
            list_name = f"{list_name_prefix} - {i:03d}"
            if list_name in list_name_to_id:
                lid = list_name_to_id[list_name]
                current_values = list_id_to_domains[lid]
                remove_items = current_values - set(domains)
                chunk = current_values - remove_items

                new_items = []
                if len(chunk) < 1000 and remaining_domains:
                    needed_items = 1000 - len(chunk)
                    new_items = list(remaining_domains)[:needed_items]
                    chunk.update(new_items)
                    remaining_domains.difference_update(new_items)

                if not chunk:
                    try:
                        delete_list(lid)
                        info(f"[−] Deleted list: {list_name} (no longer needed)")
                    except NotFoundException:
                        silent_error(f"[·] List {list_name} already gone on Cloudflare — skipping")
                    self.cache["lists"] = [l for l in self.cache["lists"] if l["id"] != lid]
                    self.cache["mapping"].pop(lid, None)
                    utils.save_cache(self.cache)
                    continue

                if remove_items or new_items:
                    try:
                        update_list(lid, remove_items, new_items)
                        info(
                            f"[~] Updated list: {list_name} "
                            f"| Added {len(new_items)}, Removed {len(remove_items)} "
                            f"| Total: {len(chunk)}"
                        )
                        self.cache["mapping"][lid] = list(chunk)
                    except NotFoundException:
                        silent_error(
                            f"[·] List {list_name} ({lid}) missing on Cloudflare "
                            f"— evicting from cache and recreating"
                        )
                        self.cache["lists"] = [l for l in self.cache["lists"] if l["id"] != lid]
                        self.cache["mapping"].pop(lid, None)
                        lst_created = create_list(list_name, list(chunk))
                        info(f"[+] Recreated list: {lst_created['name']} with {len(chunk)} domains")
                        self.cache["lists"].append(lst_created)
                        self.cache["mapping"][lst_created["id"]] = list(chunk)
                        lid = lst_created["id"]
                    utils.save_cache(self.cache)
                else:
                    silent_error(f"[·] Skipped (no changes): {list_name} | Total: {len(chunk)}")

                new_list_ids.append(lid)
            else:
                if remaining_domains:
                    needed_items = min(1000, len(remaining_domains))
                    new_items = list(remaining_domains)[:needed_items]
                    remaining_domains.difference_update(new_items)
                    lst = create_list(list_name, new_items)
                    info(f"[+] Created list: {lst['name']} with {len(new_items)} domains")
                    self.cache["lists"].append(lst)
                    self.cache["mapping"][lst["id"]] = new_items
                    utils.save_cache(self.cache)
                    new_list_ids.append(lst["id"])

        return new_list_ids

    def update_resources(self):
        info("=== [1/2] Processing BLOCK domains ===")
        block_converter = BlockDomainConverter()
        domains_to_block = block_converter.process_urls()

        info("=== [2/2] Processing ALLOW domains ===")
        # Promote AdBlock/uBlock exception rules from the block sources,
        # e.g. @@||drive.quark.cn^, into the dedicated Cloudflare Allow list.
        domains_to_allow = AllowDomainConverter().process_urls(
            extra_domains=block_converter.auto_whitelist_domains
        )

        # --- Guard: total lists must not exceed Cloudflare free tier limit ---
        block_lists_needed = (len(domains_to_block) + 999) // 1000
        allow_lists_needed = (len(domains_to_allow) + 999) // 1000
        total_lists_needed = block_lists_needed + allow_lists_needed

        info(
            f"Lists needed → Block: {block_lists_needed}, "
            f"Allow: {allow_lists_needed}, "
            f"Total: {total_lists_needed} / {MAX_TOTAL_LISTS}"
        )

        if total_lists_needed > MAX_TOTAL_LISTS:
            error(
                f"Total lists needed ({total_lists_needed}) exceeds "
                f"Cloudflare Gateway free limit of {MAX_TOTAL_LISTS} lists. "
                f"Reduce your adlists or whitelist sources."
            )

        block_priority, allow_priority, sni_priority = self._resolve_rule_priorities()
        priority_msg = (
            f"Gateway rule precedence → Allow: {allow_priority}, "
            f"Block: {block_priority}"
        )
        if sni_priority is not None:
            priority_msg += f", SNI Block: {sni_priority}"
        info(priority_msg)

        info("=== Syncing BLOCK lists & rule ===")
        # Allow rule has higher precedence (lower number = higher priority)
        self._sync_lists(
            domains_to_block,
            self.block_list_name,
            self.block_rule_name,
            rule_action="block",
            rule_priority=block_priority,
            sni_rule_name=self.block_sni_rule_name if ENABLE_SNI_FILTER else None,
            sni_rule_priority=sni_priority,
        )

        info("=== Syncing ALLOW lists & rule ===")
        self._sync_lists(
            domains_to_allow,
            self.allow_list_name,
            self.allow_rule_name,
            rule_action="allow",
            rule_priority=allow_priority,   # Lower number = evaluated first → allow wins over block
        )

        info("=== Done ===")

    def delete_resources(self):
        info("=== Deleting BLOCK resources ===")
        self._delete_rule_by_name(self.block_sni_rule_name)
        self._delete_by_prefix(self.block_list_name, self.block_rule_name)
        info("=== Deleting ALLOW resources ===")
        self._delete_by_prefix(self.allow_list_name, self.allow_rule_name)


def main():
    parser = argparse.ArgumentParser(
        description="Cloudflare Gateway DNS Filter Manager (Block + Allow)"
    )
    parser.add_argument(
        "action", choices=["run", "leave"], help="run: sync resources | leave: delete all"
    )
    args = parser.parse_args()

    cache = utils.load_cache()
    manager = CloudflareManager(cache)

    if args.action == "run":
        manager.update_resources()
        # Keep cloudflare_cache.json so actions/cache can persist the successful
        # state for the next run. Deleting it here made every run a cache miss.
    elif args.action == "leave":
        manager.delete_resources()
    else:
        error("Invalid action. Choose 'run' or 'leave'.")


if __name__ == "__main__":
    main()
