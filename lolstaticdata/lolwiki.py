from typing import Tuple, List, Optional, Dict
import os
import json
import time
import itertools
import re
import gzip
import io
from collections import Counter, defaultdict
from bs4 import BeautifulSoup
import requests
import urllib.request
import glob

from .model import Champion, Stats, Ability, AdaptiveType, AttackType, AttributeRatings, Cooldown, Cost, Effect, Price, Resource, Modifier, Role, Leveling


class UnparsableLeveling(Exception):
    pass


def grouper(iterable, n, fillvalue=None):
    """Collect champData into fixed-length chunks or blocks"""
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)


def pairwise(iterable):
    """s -> (s0,s1), (s1,s2), (s2, s3), ..."""
    a, b = itertools.tee(iterable)
    next(b, None)
    return zip(a, b)


rattribute = r"((?:[A-Za-z'\-\.0-9]+[\s\-]*)+:)"
rflat = r": (.+)"
rscaling = r"(\(\+.+?\))"
rnumber = r"(\d+\.?\d*)"
rsingle_number = r"^(\d+.?\d*)$"


def regex_slash_separated(string: str) -> List[str]:
    for i in range(20, 1, -1):
        regex = ' / '.join([rnumber for _ in range(i)])
        result = re.findall(regex, string)
        if result:
            return [eval(r) for r in result]
    raise ValueError(f"Could not parse slash-separated string: {string}")


def regex_simple_flat(string: str):
    if '/' in string:
        return regex_slash_separated(string)
    elif re.findall(rsingle_number, string):
        return eval(re.findall(rsingle_number, string)[0])
    raise ValueError(f"Could not parse a simple flat value: {string}")


class AttributeModifier(dict):
    def __init__(self, values, units):
        super().__init__({
            "values": values,
            "units": units,
        })


class Attribute(dict):
    def __init__(self, name: str, modifiers: List[AttributeModifier]):
        super().__init__({
            "attribute": name,
            "modifiers": modifiers
        })

    @classmethod
    def from_string(cls, name: str, split, verbose: bool = False):
        results = []

        # Parse out the scalings first because it works better
        print("FROM_STRING INPUT:", split)
        scalings = re.compile(rscaling).findall(split)
        if verbose:
            print("SCALINGS", scalings, split)
        for scaling in scalings:
            split = split.replace(scaling, '').strip()  # remove the scaling part of the string for processing later
        scalings = [x.strip() for x in scalings]
        for i, scaling in enumerate(scalings):
            s = Attribute._parse_scaling(scaling, num_levels=5)
            results.append(s)

        # Now parse out the flat damage info
        flat = re.compile(rflat).findall(split)
        flat = [x.strip().split(' + ') for x in flat]
        flat = [x for s in flat for x in s]  # flatten the inner split list
        if not flat:
            numbers = re.compile(rnumber).findall(split)
            # Check for the case of a just a very basic "1 / 2 / 3 / 4 / 5" string
            if split.count(" / ") == 4 and len(numbers) == 5:
                flat = [split]
            elif split.count(" / ") == 2 and len(numbers) == 3:
                flat = [split]
            # Check for the case of a just a very basic "60" string
            elif len(numbers) == 1 and str(numbers[0]) == split:
                flat = [split]
        if verbose:
            print("FLAT", flat, split)
        # assert len(flat) == 1  # don't enforce this
        for i, f in enumerate(flat):
            f = Attribute._parse_flat(f, num_levels=5)
            results.append(f)

        return cls(name=name, modifiers=results)

    @staticmethod
    def _parse_scaling(scaling, num_levels) -> AttributeModifier:
        if scaling.startswith('(') and scaling.endswith(')'):
            scaling = scaling[1:-1].strip()
        modifier = scaling[0]  # + or -
        scaling = scaling[1:].strip()
        if ' / ' in scaling:
            split = scaling.split(' / ')
        else:
            split = [scaling for _ in range(num_levels)]
        results = []
        for value in split:
            v = re.compile(rnumber).findall(value)
            if len(v) == 0:
                assert value == "Siphoning Strike stacks"
                unit = ''
                v = value
            else:
                assert len(v) >= 1  # len(v) == 1 fails on e.g. "(+ 0.5% per 100 AP)" but we still just want the first #
                v = v[0]
                assert value.startswith(v) or value.startswith(f'[ {v}')  # 2nd one is for Vi's Denting Blows: "Bonus Physical Damage: 4 / 5.5 / 7 / 8.5 / 10% (+[ 1% per 35 ][ 2.86% per 100 ]bonus AD) of target's maximum health"
                unit = value[len(v):]
                v = eval(v)
            results.append((v, unit))
        results = AttributeModifier(values=[v for v, unit in results], units=[unit for v, unit in results])
        return results

    @staticmethod
    def _parse_flat(flat, num_levels) -> AttributeModifier:
        if '(based on level)' in flat or '(based on casts)' in flat:
            if '(based on level)' in flat:
                unit = 'by level'
                flat = flat.replace('(based on level)', '').strip()
            elif '(based on casts)' in flat:
                unit = 'by cast'
                flat = flat.replace('(based on casts)', '').strip()
            else:
                raise RuntimeError("impossible")
            values = re.compile(rnumber).findall(flat)
            assert len(values) == 2
            minn = eval(values[0])
            maxx = eval(values[1])
            delta = (maxx - minn) / 17.0
            values = [minn + i*delta for i in range(18)]
            units = [unit for _ in range(18)]
            results = AttributeModifier(values=values, units=units)
            return results
        else:
            if flat.startswith('(') and flat.endswith(')'):
                flat = flat[1:-1].strip()
            if ' / ' in flat:
                split = flat.split(' / ')
            else:
                split = [flat for _ in range(num_levels)]
            results = []
            for value in split:
                v = re.compile(rnumber).findall(value)
                assert len(v) == 1
                v = v[0]
                assert value.startswith(v)
                unit = value[len(v):]
                v = eval(v)
                results.append((v, unit))

            values = [v for v, unit in results]
            units = [unit for v, unit in results]
            unique_units = set(units)
            if '' in unique_units:
                unique_units.remove('')
            if unique_units:
                assert len(unique_units) == 1
                unit = next(iter(unique_units))
                units = [unit for _ in range(len(units))]
            results = AttributeModifier(values=values, units=units)
            return results


class _Ability(dict):
    @classmethod
    def from_html(cls, champion_name: str, ability_name: str, verbose: bool = False):
        data = _Ability._pull_champion_ability(champion_name, ability_name, verbose=verbose)
        self = cls()
        self.update(data)
        return self

    @staticmethod
    def _pull_champion_ability(champion_name, ability_name, verbose: bool = False):
        ability_name = ability_name.replace(' ', '_')

        # Pull the html from the wiki
        url = f"https://leagueoflegends.fandom.com/wiki/Template:Data_{champion_name}/{ability_name}"
        html = download_webpage(url)
        soup = BeautifulSoup(html, 'html5lib')

        table = soup.find_all(['th', 'td'])

        # Set some fields to ignore
        exclude_parameters = { "callforhelp", "flavorsound", "video", "video2", "yvideo", "yvideo2", "flavor sound", "video 2", "YouTube video", 'YouTube video 2', "Not applicable to be stolen.", "Stealable", "All maps",
            # Bard
            "15", "30", "45", "55", "60", "75", "90", "100", "145", "190", "235", "280", "325", "Chimes", "3:20", "Meep limit increased to 2.", "9:10", "Slow increased to 35%.", "15:50", "Recharge time reduced to 6 seconds.", "21:40", "Recharge time reduced to 5 seconds.", "28:20", "Recharge time reduced to 4 seconds.", "34:10", "Slow increased to 75%.", "40:50", "Meep limit increased to 9.", "Displays additional information with effect table to the right.",
            # Pyke
            "25", "80", "400", "650", "800", "900", "950", "1000", "1200", "2100", "2500", "2600", "2750", "3000", "3733", "Abyssal Mask Abyssal Mask", "All maps", "Black Cleaver Black Cleaver", "32.1", "Catalyst of Aeons Catalyst of Aeons", "21.4", "Dead Man's Plate Dead Man's Plate", "13.7", "Doran's Shield Doran's Shield", "Summoner's Rift", "78.2", "Frostfang Frostfang", "Guardian's Hammer Guardian's Hammer", "Howling Abyss", "10.7", "Harrowing Crescent Harrowing Crescent", "14.3", "Infernal Mask Infernal Mask", "29.3", "Knight's Vow Knight's Vow", "Oblivion Orb Oblivion Orb", "99.3", "Phage Phage", "28.6", "Relic Shield Relic Shield", "Rod of Ages (Quick Charge) Rod of Ages (Quick Charge)", "Rylai's Crystal Scepter Rylai's Crystal Scepter", "Shurelya's Reverie Shurelya's Reverie", "5.7", "Spellthief's Edge Spellthief's Edge", "Sterak's Gage Sterak's Gage", "30.4", "Thornmail Thornmail", "72.1", "Trinity Fusion Trinity Fusion", "57.1",
            # Zoe
            "Mercurial Scimitar", "Randuin's Omen", "Hextech Protobelt-01", "Youmuu's Ghostblade", "Black Mist Scythe", "Runesteel Spaulders", "Edge of Night", "Targon's Buckler", "Pauldrons of Whiterock",
        }

        # We might want to ignore these, not sure yet
        maybe = {
            "custominfo",
            "customlabel",
            "additional",
        }

        # Do a little html modification based on the "viewsource"
        strip_table = [item.text.strip() for item in table]
        start = strip_table.index("Parameter")+3
        table = table[start:]
        return _Ability._parse_html_table(table, exclude_parameters, verbose=verbose)

    @staticmethod
    def _parse_html_table(table, exclude_parameters, verbose: bool = False):
        # Iterate over the champData in the table and parse the info
        data = {}
        for i, (parameter, value, desc) in enumerate(grouper(table, 3)):
            if not value:
                continue
            if i == 0:  # parameter is '1' for some reason but it's the ability name
                parameter = "name"
            else:
                parameter = parameter.text.strip()
            # desc = desc.text.strip()
            text = value.text.strip()
            if text and parameter not in exclude_parameters:
                data[parameter] = value

        skill = data['skill'].text.strip()
        for parameter, value in data.items():
            if parameter.startswith('leveling') and skill in ['Q', 'W', 'E', 'R']:
                try:
                    value = _Ability._parse_leveling(str(value), skill, verbose=verbose)
                except UnparsableLeveling:
                    if verbose:
                        print(f"WARNING! Could not parse: {value.text.strip()}")
                    value = value.text.strip()
                if verbose:
                    print("PARSED:", value)
                data[parameter] = value
            elif parameter in ("cooldown", "static"):
                parsed = _Ability._preparse_format(value)
                if "(based on level)" in parsed and " / " in parsed:
                    parsed = parsed.replace("(based on level)", "").strip()
                elif "(based on  Phenomenal Evil stacks)" in parsed:
                    data[parameter] = parsed
                    continue
                data[parameter] = Attribute.from_string("cooldown", parsed, verbose=verbose)
                data[parameter]["affectedByCDR"] = (parameter == "cooldown")
                del data[parameter]["attribute"]
            elif parameter == "cost":
                parsed = _Ability._preparse_format(value)
                if "10 Moonlight + 60" in parsed:
                    data[parameter] = parsed
                    continue
                data[parameter] = Attribute.from_string(parameter, parsed, verbose=verbose)
                del data[parameter]["attribute"]
            elif parameter == "recharge":
                parsed = regex_simple_flat(value)
                data[parameter] = parsed
                continue
            else:
                data[parameter] = value.text.strip()
        if verbose:
            print(data)
        if verbose:
            print()
        return data

    @staticmethod
    def _preparse_format(leveling: str):
        if not isinstance(leveling, str):
            leveling = str(leveling)
        leveling = leveling.replace('</dt>', ' </dt>')
        leveling = leveling.replace('</dd>', ' </dd>')
        leveling = BeautifulSoup(leveling, 'html5lib')
        parsed = leveling.text.strip()
        parsed = parsed.replace(u'\xa0', u' ')
        return parsed

    @staticmethod
    def _parse_leveling(leveling: str, skill: str, verbose: bool = False):
        parsed = _Ability._preparse_format(leveling)
        if verbose:
            print("PARSING LEVELING:", str(parsed))

        results = _Ability._split_leveling(parsed, verbose=verbose)

        if skill == 'R':
            if verbose:
                print("PREPARSED:", results)
            for i, attribute in enumerate(results):
                for j, modifier in enumerate(attribute['modifiers']):
                    mvalues = modifier['values']
                    if len(mvalues) == 5:
                        modifier['values'] = [mvalues[0], mvalues[2], mvalues[4]]
                    munits = modifier['units']
                    if len(munits) == 5:
                        modifier['units'] = [munits[0], munits[2], munits[4]]

        return results

    @staticmethod
    def _split_leveling(leveling: str, verbose: bool = False) -> List[Attribute]:
        # Remove some weird stuff

        leveling_removals = list()
        #  Ekko Chronobreak
        leveling_removals.append('(increased by 3% per 1% of health lost in the past 4 seconds)')

        for removal in leveling_removals:
            if removal in leveling:
                leveling = leveling.replace(removal, '').strip()

        # Split the leveling into separate attributes
        matches, splits = _Ability._match_and_split(leveling, rattribute)

        # Parse those attributes into a usable format
        results = []
        if verbose:
            print("SPLITS", splits)
        for attribute_name, split in zip(matches, splits):
            if verbose:
                print("ATTRIBUTE", attribute_name)

            attribute = Attribute.from_string(attribute_name, split, verbose=verbose)
            results.append(attribute)
        return results

    @staticmethod
    def _match_and_split(string: str, regex: str) -> Tuple[Optional[List], Optional[List]]:
        if string == "Pounce scales with  Aspect of the Cougar's rank":
            raise UnparsableLeveling(string)
        elif string == "Cougar form's abilities rank up when  Aspect of the Cougar does":
            raise UnparsableLeveling(string)
        matches = re.compile(regex).findall(string)
        matches = [match[:-1] for match in matches]  # remove the trailing :

        splits = []
        for i, m in enumerate(matches[1:], start=1):
            start = string[len(matches[i-1]):].index(m)
            split = string[:len(matches[i-1])+start].strip()
            splits.append(split)
            string = string[len(matches[i-1])+start:]
        splits.append(string)

        # Heimer has some scalings that start with numbers...
        if splits == ['Initial Rocket Magic Damage: 135 / 180 / 225 (+ 45% AP) 2-5', 'Rocket Magic Damage: 32 / 45 / 58 (+ 12% AP) 6-20', '0 Rocket Magic Damage: 16 / 22.5 / 29 (+ 6% AP)', 'Total Magic Damage: 503 / 697.5 / 892 (+ 183% AP)', ') Total Minion Magic Damage: 2700 / 3600 / 4500 (+ 900% AP)']:
            splits = ['Initial Rocket Magic Damage: 135 / 180 / 225 (+ 45% AP)', '2-5 Rocket Magic Damage: 32 / 45 / 58 (+ 12% AP)', '6-20 Rocket Magic Damage: 16 / 22.5 / 29 (+ 6% AP)', 'Total Magic Damage: 503 / 697.5 / 892 (+ 183% AP)', 'Total Minion Magic Damage: 2700 / 3600 / 4500 (+ 900% AP)']

        return matches, splits



def main():
    directory = os.path.dirname(os.path.realpath(__file__))

    statsfn = directory + "/champData/champion_stats.json"
    stats = pull_all_champion_stats()
    save_json(stats, statsfn)

    with open(statsfn) as f:
        stats = json.load(f)

    # Missing skills
    missing_skills = {
        "Annie": ["Command Tibbers"] ,
        "Jinx": ["Switcheroo! 2"] ,
        "Nidalee": ["Aspect of the Cougar 2"] ,
        "Pyke": ["Death from Below 2"],
        "Rumble": ["Electro Harpoon 2"] ,
        "Shaco": ["Command Hallucinate"] ,
        "Syndra": ["Force of Will 2"] ,
        "Taliyah": ["Seismic Shove 2"],
    }

    for champion_name, details in stats.items():
        jsonfn = directory + f"/champData/_{details['apiname']}.json"
        #if os.path.exists(jsonfn):
        #    continue
        print(champion_name)
        if champion_name == "Kled & Skaarl":
            champion_name = "Kled"
        for ability in ['i', 'q', 'w', 'e', 'r']:
            result = {}
            for ability_name in details[f"skill_{ability}"].values():
                if champion_name in missing_skills and ability_name in missing_skills[champion_name]:
                    continue
                print(ability_name)
                r = _Ability.from_html(champion_name, ability_name, verbose=True)
                # check to see if this ability was already pulled
                found = False
                for r0 in result.values():
                    if r == r0:
                        found = True
                if not found:
                    result[ability_name] = r
            details[f"skill_{ability}"] = result
        save_json(details, jsonfn)
        print()



def rename_keys(j):
    map = {
        "id": "id",
        "champion": "champion",
        "skill": "skill",
        "name": "name",
        "apiname": "name",
        "fullname": "fullName",
        "nickname": "nickname",
        "disp_name": "dispName",
        "occurrence": "occurrence",
        "collision radius": "collisionRadius",
        "tether radius": "tetherRadius",
        "recharge": "recharge",
        "customlabel": "customLabel",
        "custominfo": "customInfo",
        "ontargetcdstatic": "onTargetCDStatic",
        "inner radius": "innerRadius",
        "onhiteffects": "onHitEffects",
        "title": "title",
        "attack": "attack",
        "defense": "defense",
        "magic": "magic",
        "difficulty": "difficulty",
        "herotype": "class",
        "alttype": "altType",
        "resource": "resource",
        "stats": "stats",
        "hp_base": "healthBase",
        "hp_lvl": "healthPerLevel",
        "mp_base": "manaBase",
        "mp_lvl": "manaPerLevel",
        "arm_base": "armorBase",
        "arm_lvl": "armorPerLevel",
        "mr_base": "magicResistBase",
        "mr_lvl": "magicResistPerLevel",
        "hp5_base": "healthPer5Base",
        "hp5_lvl": "healthPer5PerLevel",
        "mp5_base": "manaPer5Base",
        "mp5_lvl": "manaPer5PerLevel",
        "dam_base": "attackDamageBase",
        "dam_lvl": "attackDamagePerLevel",
        "as_base": "attackSpeedBase",
        "as_lvl": "attackSpeedPerLevel",
        "crit_base": "criticalStrikeBase",
        "crit_mod": "criticalStrikeModifier",
        "missile_speed": "missileSpeed",
        "attack_cast_time": "attackCastTime",
        "attack_total_time": "attackTotalTime",
        "windup_modifier": "windupModifier",
        "urf_dmg_dealt": "urfDamageDealt",
        "urf_dmg_taken": "urfDamageTaken",
        "urf_healing": "urfHealing",
        "urf_shielding": "urfShielding",
        "aram_dmg_dealt": "aramDamageDealt",
        "aram_dmg_taken": "aramDamageTaken",
        "aram_healing": "aramHealing",
        "aram_shielding": "aramShielding",
        "static": "staticCooldown",
        "icon": "icon",
        "icon2": "icon2",
        "icon3": "icon3",
        "icon4": "icon4",
        "icon5": "icon5",
        "gameplay_radius": "gameplayRadius",
        "blurb": "blurb",
        "description": "description",
        "description2": "description2",
        "description3": "description3",
        "description4": "description4",
        "description5": "description5",
        "leveling": "leveling",
        "leveling2": "leveling2",
        "leveling3": "leveling3",
        "leveling4": "leveling4",
        "leveling5": "leveling5",
        "targeting": "targeting",
        "projectile": "projectile",
        "speed": "speed",
        "cost": "cost",
        "Cost": "cost",
        "costtype": "resource",
        "affects": "affects",
        "effect radius": "effectRadius",
        "damagetype": "damageType",
        #"spelleffects": "spellEffects",
        "spellshield": "spellshieldable",
        "width": "width",
        "angle": "angle",
        "cast time": "castTime",
        "attribute": "attribute",
        "values": "values",
        "units": "units",
        "modifiers": "modifiers",
        "cooldown": "cooldown",
        "notes": "notes",
        "range": "attackRange",
        "range_lvl": "attackRangePerLevel",
        "ms": "movespeed",
        "acquisition_radius": "acquisitionRadius",
        "selection_radius": "selectionRadius",
        "pathing_radius": "pathingRadius",
        "as_ratio": "attackSpeedRatio",
        "attack_delay_offset": "attackDelayOffset",
        "rangetype": "attackType",
        "target range": "targetRange",
        "date": "releaseDate",
        "patch": "releasePatch",
        "changes": "patchLastChanged",
        "role": "roles",
        "damage": "damage",
        "toughness": "toughness",
        "control": "control",
        "mobility": "mobility",
        "utility": "utility",
        "style": "abilityReliance",
        "adaptivetype": "adaptiveType",
        "be": "blueEssence",
        "rp": "rp",
        "skill_i": "skillP",
        "skill_q": "skillQ",
        "skill_w": "skillW",
        "skill_e": "skillE",
        "skill_r": "skillR",
        "secondary attributes": "secondaryAttributes",
        "affectedByCDR": "affectedByCDR",
    }

    new = {}
    for key, value in j.items():
        if key == '':
            continue
        if key.startswith("skill_"):
            value = list(value.values())
        elif key == "skill" and value == "I":
            value = "P"

        if isinstance(value, dict):
            value = rename_keys(value)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    item = rename_keys(item)
                    value[i] = item
        try:
            new_key = map[key]
            new[new_key] = value
        except:
            print(key, value)
            new_key = map[key]
            new[new_key] = value
    return new

def reformat_json_after_renaming(new):
    new["name"] = new["skillQ"][0]["champion"]

    new["attributeRatings"] = {}
    if "damage" in new:
        new["attributeRatings"]["damage"] = new["damage"]
        del new["damage"]
    if "toughness" in new:
        new["attributeRatings"]["toughness"] = new["toughness"]
        del new["toughness"]
    if "control" in new:
        new["attributeRatings"]["control"] = new["control"]
        del new["control"]
    if "mobility" in new:
        new["attributeRatings"]["mobility"] = new["mobility"]
        del new["mobility"]
    if "utility" in new:
        new["attributeRatings"]["utility"] = new["utility"]
        del new["utility"]
    if "abilityReliance" in new:
        new["attributeRatings"]["abilityReliance"] = new["abilityReliance"]
        del new["abilityReliance"]
    if "attack" in new:
        new["attributeRatings"]["attack"] = new["attack"]
        del new["attack"]
    if "defense" in new:
        new["attributeRatings"]["defense"] = new["defense"]
        del new["defense"]
    if "magic" in new:
        new["attributeRatings"]["magic"] = new["magic"]
        del new["magic"]
    if "difficulty" in new:
        new["attributeRatings"]["difficulty"] = new["difficulty"]
        del new["difficulty"]

    if "class" in new and "roles" in new:
        new["roles"].append(new["class"])
        del new["class"]
    if "altType" in new and "roles" in new:
        new["roles"].append(new["altType"])
        del new["altType"]
    if "roles" in new:
        new["roles"] = set(new["roles"])

    new["abilities"] = {
        "passive": [],
        "q": [],
        "w": [],
        "e": [],
        "r": []
    }
    for skill in new["skillP"]:
        del skill["champion"]
        del skill["skill"]
        new["abilities"]["passive"].append(skill)
    for skill in new["skillQ"]:
        del skill["champion"]
        del skill["skill"]
        new["abilities"]["q"].append(skill)
    for skill in new["skillW"]:
        del skill["champion"]
        del skill["skill"]
        new["abilities"]["w"].append(skill)
    for skill in new["skillE"]:
        del skill["champion"]
        del skill["skill"]
        new["abilities"]["e"].append(skill)
    for skill in new["skillR"]:
        del skill["champion"]
        del skill["skill"]
        new["abilities"]["r"].append(skill)
    del new["skillP"]
    del new["skillQ"]
    del new["skillW"]
    del new["skillE"]
    del new["skillR"]
    # abilities now has format: "abilities": {"passive": [...], "q": [...], ...}

    # Capitalize enums (and snake-case)
    new["resource"] = new["resource"].upper()
    new["roles"] = [role.upper() for role in new["roles"]]
    new["attackType"] = new["attackType"].upper()
    new["adaptiveType"] = new["adaptiveType"].upper()
    def to_enum(string):
        return string.strip().upper().replace(' ', '_').replace('-', '_')
    for _, skills in new["abilities"].items():
        for skill in skills:
            #skill["attribute"] = skill["attribute"].upper()  # TODO: This is for leveling*
            if "targeting" in skill:
                skill["targeting"] = [to_enum(t) for t in skill["targeting"].split('/')]
            if "affects" in skill:
                skill["affects"] = [to_enum(affect) for affect in skill["affects"].split(',')]
            if "spellshieldable" in skill:
                skill["spellshieldable"] = to_enum(skill["spellshieldable"])  # true/false/special
            if "resource" in skill:
                skill["resource"] = to_enum(skill["resource"])
            if "damageType" in skill:
                skill["damageType"] = to_enum(skill["damageType"])
            #if "spellEffects" in skill:
            #    skill["spellEffects"] = to_enum(skill["spellEffects"])
            if "projectile" in skill:
                skill["projectile"] = to_enum(skill["projectile"])  # true/false/special/yasuo
            if "onHitEffects" in skill:
                skill["onHitEffects"] = to_enum(skill["onHitEffects"])
            if "occurrence" in skill:
                skill["occurrence"] = to_enum(skill["occurrence"])

            if "damageType" in skill:
                if '/' in skill["damageType"]:
                    skill["damageType"] = "MIXED_DAMAGE"
                elif skill["damageType"] == "PHYSICAL":
                    skill["damageType"] = "PHYSICAL_DAMAGE"
                elif skill["damageType"] == "MAGIC":
                    skill["damageType"] = "MAGIC_DAMAGE"
                elif skill["damageType"] == "TRUE":
                    skill["damageType"] = "TRUE_DAMAGE"
                elif skill["damageType"] == "PURE":
                    skill["damageType"] = "PURE_DAMAGE"
                else:
                    skill["damageType"] = "OTHER"

            if "resource" in skill:
                if skill["resource"] in ("MANA", "NO_COST", "HEALTH", "MAXIMUM_HEALTH", "ENERGY", "CURRENT_HEALTH", "HEALTH_PER_SECOND", "MANA_PER_SECOND", "CHARGE", "FURY"):
                    pass
                elif skill["resource"] in (
                        'MANA_+_4_FOCUS',
                        'MANA_+_4_FROST_STACKS',
                        'MANA_+_6_CHARGES',
                        'MANA_+_1_SAND_SOLDIER',
                        'MANA_+_40_/_45_/_50_/_55_/_60_PER_SECOND',
                        'MAXIMUM_HEALTH_+_50_/_55_/_60_/_65_/_70_MANA',
                        'MANA_+_1_TURRET_KIT',
                        'MANA_+_1_MISSILE',
                        'MANA_+_1_CHARGE',
                        'MANA_+_ALL_CHARGES',
                ):
                    skill["resource"] = "MANA"
                elif skill["resource"] == 'OF_CURRENT_HEALTH':
                    skill["resource"] = "CURRENT_HEALTH"
                elif skill["resource"] == '%_OF_CURRENT_HEALTH':
                    skill["resource"] = "CURRENT_HEALTH"
                elif skill["resource"] == 'CURRENT_GRIT':
                    skill["resource"] = "GRIT"
                elif skill["resource"] == "CURRENT_FURY":
                    skill["resource"] = "FURY"
                elif skill["resource"] == 'FURY_EVERY_0.5_SECONDS':
                    skill["resource"] = "FURY"
                else:
                    skill["resource"] = "OTHER"

            # Change the skill descriptions + levelings format to:
            # "effects": [
            #     {"description": ..., "leveling": ..., "icon": ...},
            #     {"description": ..., "leveling": ..., "icon": ...},
            #     ...
            # ]
            skill["effects"] = []
            for ending in ['', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10']:
                d = f"description{ending}"
                i = f"icon{ending}"
                l = f"leveling{ending}"
                item = {}
                if d in skill:
                    item["description"] = skill[d]
                    del skill[d]
                if i in skill:
                    item["icon"] = skill[i]
                    del skill[i]
                if l in skill:
                    item["leveling"] = skill[l]
                    del skill[l]
                if item:
                    skill["effects"].append(item)

            if "notes" in skill and skill["notes"] == "* No additional notes.":
                del skill["notes"]

    if new["adaptiveType"] in ("PHYSICAL", "MIXED,PHYSICAL"):
        new["adaptiveType"] = "PHYSICAL_DAMAGE"
    if new["adaptiveType"] == "MAGIC":
        new["adaptiveType"] = "MAGIC_DAMAGE"

    # Remove the leading V
    if "releasePatch" in new:
        new["releasePatch"] = new["releasePatch"][1:]
    if "patchLastChanged" in new:
        new["patchLastChanged"] = new["patchLastChanged"][1:]

    new["price"] = {}
    if "blueEssence" in new:
        new["price"]["blueEssence"] = new["blueEssence"]
    if "rp" in new:
        new["price"]["rp"] = new["rp"]

    return new


def rename_all():
    directory = os.path.dirname(os.path.realpath(__file__))
    files = sorted(glob.glob(directory + "/champData/_**.json"))
    for fn in files:
        with open(fn) as f:
            j = json.load(f)
        renamed = rename_keys(j)
        renamed = reformat_json_after_renaming(renamed)
        new_fn = fn.replace('champData/_', 'champData/')
        save_json(renamed, new_fn)


def capture_enums():
    directory = os.path.dirname(os.path.realpath(__file__))
    files = sorted(glob.glob(directory + "/champData/**.json"))
    files = [f for f in files if '_' not in f]

    enums = defaultdict(list)
    for fn in files:
        with open(fn) as f:
            j = json.load(f)
            _enums = _capture_enums(j)
            for k, v in _enums.items():
                enums[k].extend(v)
    enums = {k: set(v) for k, v in enums.items()}
    return enums


def _capture_enums(j) -> Dict:
    enums = defaultdict(list)
    for key, value in j.items():
        if isinstance(value, dict):
            _enums = _capture_enums(value)
            for k, v in _enums.items():
                enums[k].extend(v)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _enums = _capture_enums(item)
                    for k, v in _enums.items():
                        enums[k].extend(v)
        else:
            enums[key].append(value)
            #if key == "recharge":
            #    print(key, value, j["name"])
    return enums



if __name__ == "__main__":
    main()
    rename_all()
    enums = capture_enums()
    #print(enums['resource'])
    #for k, v in enums.items():
    #    print(k, v)



"""

* Collect all enums and their values into a file that we can distribute.

* Leave `effectRadius` as a string. Shyvana's Burnout was the deciding factor for me. I'm going to do the same for `width`, `angle`, `castType`, and `speed`.

* There are a few skills in `leveling4` and `leveling` that could be parsed correctly, but that's an overall problem with parsing that we need to fix.

"""
