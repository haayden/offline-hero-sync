"""Rebuild an online hero's full save from the running game (read-only).  New in 1.1.

Since about 2026-10-03 the game no longer receives an online hero's save file: Mojang's servers keep it
and stream the pieces the game needs.  Everything a save holds is still in the game's memory while the hero
is in the world, spread over components, so the save is assembled from those:

  MetaData            level/power from the hero, the rest from the previous copy (or defaults)
  Ability             saved attributes (emeralds, echo shards, XP, level, upgrade levels, ...)
  Achievements        AchievementSystemComponent: each achievement's condition (count / collection /
                      quest state); bool achievements only carry over (their state is not replicated)
  CollectionsStats    hints and expandable info from CollectionsStatsComponent; the collected-item
                      lists are not replicated (only counts), so: previous copy + every owned item
  Cosmetics           SWPaperdollComponent.SlotMap
  Inventory           the in-world reader's items (offline_hero_sync.Live)
  LootProgression     LootProgressionComponent.LootItemsCollected
  quest               BP_QuestRunner: every quest, its tasks in save order, CompletedTaskCount
  WorldExploration    WorldExplorationComponent (stations, doors, fog of war, gimmicks, cutscenes);
                      SavedActorStates are server-only, so they carry over from the previous copy

Every read goes through uemem's live reflection (field offsets from the running build).
"""
import copy
import re
import zlib

SAVED_ATTRS = ('VillageMerchantRefreshCharges', 'VillageMerchantUpgradeLevel', 'EnchantsmithUpgradeLevel',
               'OldBlacksmithUpgradeLevel', 'Emeralds', 'SpringStone', 'Level', 'XP', 'EnchantmentPoints')
DEFAULT_META = {'Created': 0, 'CurrentLocation': 'SW.Area.Town', 'ReleaseToggles': ['R1', 'R2'],
                'CurrentDifficulty': 'None'}
COMPONENTS = ['AttributeSet', 'InventoryManagerComponent', 'SWPaperdollComponent', 'LootProgressionComponent',
              'CollectionsStatsComponent', 'AchievementSystemComponent', 'WorldExplorationComponent']
COLLECTION_LISTS = ['CollectedWeaponsUnique', 'CollectedWeaponsSpecial', 'CollectedWeaponsRare',
                    'CollectedWeaponsCommon', 'CollectedArmorUnique', 'CollectedArmorSpecial', 'CollectedArmorRare',
                    'CollectedArmorCommon', 'CollectedArtifactsUnique', 'CollectedArtifactsSpecial',
                    'CollectedArtifactsRare', 'CollectedArtifactsCommon', 'CollectedTalismans',
                    'CollectedEnchantmentBooksOffensive', 'CollectedEnchantmentBooksDefensive',
                    'CollectedEnchantmentBooksUtility', 'CollectedEnchantmentBooksUncategorised']
# enchantment book -> collection list (from saves the game wrote; anything new goes to Uncategorised)
BOOK_CATEGORY = {
    'Piercing': 'Offensive', 'PoisonFog': 'Offensive', 'Borealis': 'Offensive', 'SoulAspect': 'Offensive',
    'Thundering': 'Offensive', 'Blowback': 'Offensive', 'FrostCrescent': 'Offensive', 'Unstoppable': 'Offensive',
    'Channeling': 'Offensive', 'Shockwave': 'Offensive', 'FireAspect': 'Offensive', 'Ricochet': 'Offensive',
    'BurstBowstring': 'Offensive', 'Swirling': 'Offensive', 'ChainReaction': 'Offensive',
    'Radiance': 'Defensive', 'PotionBarrier': 'Defensive', 'GuardingStrike': 'Defensive',
    'SpringLoaded': 'Utility', 'Dynamo': 'Utility', 'SoulInfusedPotion': 'Utility', 'PotionSharing': 'Utility',
    'TempoTheft': 'Utility', 'MultiRoll': 'Utility', 'LingeringPower': 'Utility', 'CriticalQuiver': 'Utility',
    'ExpandedQuiver': 'Utility', 'HealthSynergy': 'Utility', 'Arcane': 'Utility', 'GravityPulse': 'Utility',
    'MultiPotion': 'Utility', 'ShadowStrike': 'Utility'}
ARMOR_RE = re.compile(r'(Helmet|Chest|Leggings|Boots|Armor)(_|\d|$)')
RARITY_SUFFIX = {'SW.Rarity.Unique': 'Unique', 'SW.Rarity.Special': 'Special', 'SW.Rarity.Rare': 'Rare',
                 'SW.Rarity.Common': 'Common'}


class RebuildError(Exception):
    pass


class Rebuilder:
    def __init__(self, live, ohs):
        """live: offline_hero_sync.Live (in-world reader); ohs: the offline_hero_sync module (helpers)."""
        self.live, self.ue, self.ohs = live, live.ue, ohs
        self._key = None
        self._found = None

    # ---- object discovery (one walk over GUObjectArray, cached while the pawn lives) -----------------------
    def _discover(self, pc, pawn, ps):
        ue = self.ue
        key = (pc, pawn, ps)
        if self._key == key and self._found:
            return self._found
        owners = {pc, pawn, ps} - {0, None}
        targets = {ue.find_type(c): c for c in COMPONENTS}
        targets.pop(None, None)
        found = {c: [] for c in COMPONENTS}
        runners = []
        for o in ue.iter_objects():
            try:
                c = ue.cls(o)
                if ue.outer(o) in owners:
                    x = c
                    while x:
                        if x in targets:
                            found[targets[x]].append(o)
                            break
                        x = ue.q(x + 0x40)
                elif 'QuestRunner' in ue.objname(c) and not ue.objname(o).startswith('Default__'):
                    if 'Quests' in ue.oprops(o):
                        runners.append(o)
            except (MemoryError, UnicodeDecodeError):
                continue
        found['QuestRunner'] = runners
        self._key, self._found = key, found
        return found

    def _one(self, found, name):
        objs = found.get(name) or []
        if not objs:
            raise RebuildError('%s not found' % name)
        return objs[0]

    def _get(self, o, field):
        p = self.ue.oprops(o).get(field)
        if p is None:
            raise RebuildError('%s has no %s (game updated?)' % (self.ue.clsname(o), field))
        return p, o + p.offset

    def _val(self, o, field):
        p, a = self._get(o, field)
        return self.w.value(p, a)

    def _obj(self, a):
        return self.ue.q(a)

    def _struct_items(self, o, field, inner_field):
        """FFastArraySerializer-style struct -> [(inner Prop, element addr)]."""
        p, a = self._get(o, field)
        props = {q.name: q for q in self.w.ordered_props(p.struct)}
        arr = props.get(inner_field)
        if arr is None:
            raise RebuildError('%s.%s has no %s' % (self.ue.clsname(o), field, inner_field))
        inner = arr.inner
        return [(inner, e) for e in self.ue.tarray(a + arr.offset, inner.size)]

    # ---- sections ------------------------------------------------------------------------------------------
    def build(self, online_id, base=None):
        """-> (save dict shaped like the server copy, with IsOnline true; notes list)."""
        import savejson
        self.w = savejson.Writer(self.ue, objects=True)
        pl = self.live.player()
        if not pl:
            raise RebuildError('no local player')
        pc, pawn, ps, cid = pl
        if cid != online_id:
            raise RebuildError('hero %s is not the one in the world' % online_id[:8])
        if not pawn or not self.ue.is_a(pawn, 'Character'):
            raise RebuildError('hero not loaded in the world')
        found = self._discover(pc, pawn, ps)
        notes = []
        b = copy.deepcopy(base['CharacterSaveV1']) if base else None
        attrs = self.live.attributes(found['AttributeSet'])
        if not attrs:
            raise RebuildError('no attribute sets on the hero')
        inv = self.live.inventory(found['InventoryManagerComponent'])
        if not inv:
            raise RebuildError('no inventory entries on the hero')

        c = {}
        c['MetaData'] = self.metadata(online_id, attrs, b)
        c['Ability'] = self.ability(attrs, b)
        quest = self.quests(self._one(found, 'QuestRunner'), ps, b)       # first: achievements use quest states
        c['Achievements'] = self.achievements(self._one(found, 'AchievementSystemComponent'), b, notes)
        c['CollectionsStats'] = self.collections(self._one(found, 'CollectionsStatsComponent'), inv, b)
        c['Cosmetics'] = self.cosmetics(self._one(found, 'SWPaperdollComponent'))
        c['Inventory'] = self.inventory(inv, b, c['MetaData'], notes)
        c['LootProgression'] = {'DiscoveredLoot': self._val(self._one(found, 'LootProgressionComponent'),
                                                            'LootItemsCollected')}
        c['quest'] = quest
        c['WorldExploration'] = self.exploration(self._one(found, 'WorldExplorationComponent'), b, notes)
        # same key order as the game's own saves
        order = ['MetaData', 'Ability', 'Achievements', 'CollectionsStats', 'Cosmetics', 'Inventory',
                 'LootProgression', 'quest', 'WorldExploration']
        if b:
            order = [k for k in b if k in c] + [k for k in order if k not in b]
        save = {'SerializeMeta': dict(base['SerializeMeta']) if base else dict(savejson.SERIALIZE_META),
                'CharacterSaveV1': {k: c[k] for k in order}}
        return save, notes

    def metadata(self, online_id, attrs, b):
        md = dict(b['MetaData']) if b else dict(DEFAULT_META)
        md['CharacterId'] = online_id
        md['IsOnline'] = True
        md['IsGuest'] = False
        lvl = attrs.get('Level')
        if lvl:
            md['Level'] = int(round(lvl[1]))
        md.setdefault('Level', 1)
        md.setdefault('PowerLevel', 1)
        md['GameDataUpdated'] = self.ohs.now_ticks()
        order = ['CharacterId', 'Created', 'GameDataUpdated', 'IsOnline', 'IsGuest', 'Level', 'PowerLevel',
                 'CurrentLocation', 'ReleaseToggles', 'CurrentDifficulty']
        return {k: md[k] for k in order if k in md} | {k: v for k, v in md.items() if k not in order}

    def ability(self, attrs, b):
        names = [a['AttributeName'] for a in b['Ability']['Attributes']] if b else list(SAVED_ATTRS)
        prev = {a['AttributeName']: a['CurrentValue'] for a in b['Ability']['Attributes']} if b else {}
        out = []
        for n in names:
            v = attrs.get(n)
            if v is not None:
                out.append({'AttributeName': n, 'CurrentValue': self.ohs.num(v[1])})
            elif n in prev:
                out.append({'AttributeName': n, 'CurrentValue': prev[n]})
        return {'Attributes': out, 'ProgressionTags': b['Ability']['ProgressionTags'] if b else []}

    def achievements(self, comp, b, notes):
        ue = self.ue
        out = {'QuestAchievements': {}, 'BoolAchievements': {}, 'CollectionAchievements': {},
               'CountAchievements': {}}
        if b:
            order = list(b['Achievements'])
            out = {k: dict(b['Achievements'].get(k, {})) for k in order} | \
                  {k: v for k, v in out.items() if k not in order}
        quest_states = self._quest_states
        for _, e in self._struct_items(comp, 'AchievementList', 'Entries'):
            props = {q.name: q for q in self.w.ordered_props(self._entry_struct(comp, 'AchievementList',
                                                                                'Entries'))}
            ach = self._obj(e + props['Achievement'].offset)
            if not ach:
                continue
            tag = self._val(ach, 'Tag')
            cond = self._obj(self._get(ach, 'Condition')[1])
            if not cond or tag in ('', 'None'):
                continue
            kind = ue.clsname(cond)
            if kind.startswith('ConditionCount'):
                n = self._val(cond, 'CurrentAmount')
                if n:
                    out['CountAchievements'][tag] = {'Count': n}
            elif kind.startswith('ConditionCollection'):
                tags = self._val(cond, 'CollectedTags')
                if tags:
                    out['CollectionAchievements'][tag] = {'CollectedTags': tags}
            elif kind.startswith('ConditionQuest'):
                qs = self._val(cond, 'QuestState')
                done = quest_states.get(qs.get('QuestName')) == qs.get('QuestState') and qs.get('QuestState')
                if done or self._val(cond, 'bCompleted'):
                    out['QuestAchievements'][tag] = {'bCompleted': True}
            elif kind.startswith('ConditionBool'):
                if self._val(cond, 'bCompleted'):
                    out['BoolAchievements'][tag] = {'bCompleted': True}
        return out

    def _entry_struct(self, o, field, inner_field):
        p, _ = self._get(o, field)
        props = {q.name: q for q in self.w.ordered_props(p.struct)}
        return props[inner_field].inner.struct

    def collections(self, comp, inv, b):
        prev = b['CollectionsStats'] if b else {}
        lists = {k: list(prev.get(k, [])) for k in COLLECTION_LISTS}
        for l in inv:
            t, r = l['TypeTag'], l['RarityTag']
            name = collection_list(t, r)
            if name and t not in lists[name]:
                lists[name].append(t)
        od = self._val(comp, 'OnboardingData')
        out = {k: lists[k] for k in COLLECTION_LISTS}
        out['ShownHints'] = od.get('ShownHints', prev.get('ShownHints', []))
        out['SeenExpandableInfo'] = od.get('SeenExpandableInfo', prev.get('SeenExpandableInfo', []))
        if b:   # keep the game's key order and anything it added
            out = {k: out.get(k, prev[k]) for k in prev} | {k: v for k, v in out.items() if k not in prev}
        return out

    def cosmetics(self, comp):
        out = {}
        for e in self._val(comp, 'SlotMap'):
            out[e['Key']] = {'TypeTag': e['Value']['TypeTag']}
        return {'Cosmetics': out}

    def inventory(self, inv, b, md, notes):
        """Items from the game.  The server no longer sends GenesisRandomSeed or the hidden power-roll values
        (PlayerLevel, threat, ItemPowerMin/Max, RNGRoll), so they come from the previous copy's entry for the
        same item (matched by TypeTag + PickupTimestamp, unique per item); new items get a stable seed and the
        values the game gives freshly generated gear."""
        ohs = self.ohs
        entries = b['Inventory']['Entries'] if b else []
        by_key = {(e['ItemData']['TypeTag'], e['ItemData']['PickupTimestamp']): (i, e) for i, e in enumerate(entries)}
        no_layout = {'layout': {}}
        level = md['Level']
        out, matched = [], 0
        for l in inv:
            hit = by_key.get((l['TypeTag'], l['PickupTimestamp']))
            if hit:
                matched += 1
                l = dict(l, Seed=hit[1]['ItemData']['GeneratorData']['GenesisRandomSeed'])
                out.append((hit[0], ohs.live_to_entry(l, hit[1], no_layout, level)))
            else:
                if not l['Seed']:
                    l = dict(l, Seed=zlib.crc32(('%s|%d|%d' % (l['TypeTag'], l['PickupTimestamp'], l['uid'])).encode()))
                out.append((len(entries) + len(out), ohs.live_to_entry(l, None, no_layout, level)))
        out = [e for _, e in sorted(out, key=lambda x: x[0])]
        if entries:
            notes.append('items: %d kept their seed from the previous copy, %d new' % (matched, len(inv) - matched))
        pw = [e['ItemData']['GeneratorData']['PowerGeneratorValues']['ItemPower'] for e in out
              if e['EquippedSlot'].startswith(ohs.POWER_SLOTS)]
        if pw:
            md['PowerLevel'] = int(sum(pw) // len(pw))
        return {'Entries': out}

    def quests(self, runner, ps, b=None):
        ue = self.ue
        prev = {q['QuestName']: q for q in b['quest']['Quests']} if b else {}
        quests = []
        states = {}
        for e in ue.tarray(self._get(runner, 'Quests')[1], 8):
            q = self._obj(e)
            if not q:
                continue
            name = self._val(q, 'ID')
            state = self._val(q, 'State').get('State')
            for d in self._val(q, 'PlayerSpecificQuestData') if 'PlayerSpecificQuestData' in ue.oprops(q) else []:
                if d.get('SavedQuestState'):
                    state = d['SavedQuestState']
            tasks = []
            root = self._obj(self._get(q, 'Tasks')[1])
            if root:
                self._walk_tasks(root, tasks)
            old = {t['TaskName']: t for t in prev.get(name, {}).get('TaskData', [])}
            for i, t in enumerate(tasks):
                # finished repeatable quests reset their task objects to NotSet in-world while the save keeps
                # them done; branches a quest did not take stay NotSet in the save too, so only the previous
                # copy can tell them apart
                o = old.get(t['TaskName'])
                if t['State'] == 'NotSet' and state == 'Completed' and o and o['State'] != 'NotSet':
                    tasks[i] = dict(o)
            if old:   # the previous copy's task order (the game's own), new tasks after
                idx = {n: i for i, n in enumerate(old)}
                tasks.sort(key=lambda t: idx.get(t['TaskName'], len(idx)))
            states[name] = state
            quests.append({'QuestName': name, 'State': state, 'TaskData': tasks})
        if prev:
            idx = {n: i for i, n in enumerate(prev)}
            quests.sort(key=lambda q: idx.get(q['QuestName'], len(idx)))
        self._quest_states = states
        focused = ''
        if 'PlayerFocusedQuests' in ue.oprops(runner):
            p, a = self._get(runner, 'PlayerFocusedQuests')
            inner = p.inner
            props = {x.name: x for x in self.w.ordered_props(inner.struct)}
            for el in ue.tarray(a, inner.size):
                if ue.q(el + props['PlayerState'].offset) == ps:
                    focused = ue.fstring(el + props['questId'].offset)
        return {'Quests': quests, 'FocusedQuestId': focused}

    def _walk_tasks(self, t, out):
        """Children first, then the list itself (the order the game saves them in)."""
        props = self.ue.oprops(t)
        if 'Tasks' in props and props['Tasks'].kind == 'ArrayProperty':
            for e in self.ue.tarray(t + props['Tasks'].offset, 8):
                ch = self._obj(e)
                if ch:
                    self._walk_tasks(ch, out)
        out.append({'TaskName': self._val(t, 'ID'), 'State': self._val(t, 'State').get('State'),
                    'PartialProgress': self._val(t, 'CompletedTaskCount')})

    def exploration(self, comp, b, notes):
        prev = b['WorldExploration'] if b else {}
        fow = []
        for inner, e in self._struct_items(comp, 'FowAreaData', 'Items'):
            d = self.w.struct(inner.struct, e)
            tag = d.get('Tag')
            fow.append({'Tag': tag.get('Tag') if isinstance(tag, dict) else tag, 'Data': d.get('Data', []),
                        'WorldPosition': d.get('WorldPosition'), 'Size': d.get('Size')})
        live_states = self._val(comp, 'SavedActorStates')
        states = prev.get('SavedActorStates', [])
        if live_states:
            notes.append('actor states: %d live' % len(live_states))
        out = {
            'DiscoveredMinecartStationTags': self._val(comp, 'DiscoveredMinecartStations'),
            'ActivatedGimmickTags': self._val(comp, 'ActivatedGimmicks') or prev.get('ActivatedGimmickTags', []),
            'SavedCutsceneTags': self._val(comp, 'PlayedCutscenes') or prev.get('SavedCutsceneTags', []),
            'SavedFogOfWarExploration': {'Items': fow},
            'DiscoveredDungeonDoors': self._val(comp, 'DiscoveredDungeonDoors'),
            'LastMinecartStation': self._val(comp, 'LastMinecartStationAccessed'),
            'SavedActorStates': states,
        }
        if prev:
            out = {k: out.get(k, prev[k]) for k in prev} | {k: v for k, v in out.items() if k not in prev}
        return out


def collection_list(tag, rarity):
    if tag.startswith('SW.Item.EnchantmentBook.'):
        return 'CollectedEnchantmentBooks' + BOOK_CATEGORY.get(tag.rsplit('.', 1)[-1], 'Uncategorised')
    if tag.startswith('SW.Item.Talisman.'):
        return 'CollectedTalismans'
    suffix = RARITY_SUFFIX.get(rarity)
    if not suffix or not tag.startswith('SW.Item.') or '.Cosmetic.' in tag:
        return None
    if tag.startswith('SW.Item.Artifact.'):
        return 'CollectedArtifacts' + suffix
    if ARMOR_RE.search(tag.rsplit('.', 1)[-1]):
        return 'CollectedArmor' + suffix
    return 'CollectedWeapons' + suffix
