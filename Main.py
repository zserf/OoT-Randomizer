from collections import OrderedDict
from itertools import zip_longest
import json
import logging
import platform
import random
import subprocess
import time
import os

from BaseClasses import World, CollectionState, Item
from Regions import create_regions
from EntranceShuffle import link_entrances
from Rom import patch_rom, LocalRom
from Rules import set_rules
from Dungeons import create_dungeons, fill_dungeons_restrictive
from Fill import distribute_items_restrictive
from ItemList import generate_itempool
from Utils import default_output_path
from version import __version__

def main(settings):
    start = time.clock()

    # initialize the world

    world = World(settings)

    logger = logging.getLogger('')

    random.seed(world.numeric_seed)

    logger.info('OoT Randomizer Version %s  -  Seed: %s\n\n', __version__, world.seed)

    create_regions(world)

    create_dungeons(world)

    logger.info('Shuffling the World about.')

    link_entrances(world)

    logger.info('Calculating Access Rules.')

    set_rules(world)

    logger.info('Generating Item Pool.')

    generate_itempool(world)

    logger.info('Placing Dungeon Items.')

    shuffled_locations = None
    shuffled_locations = world.get_unfilled_locations()
    random.shuffle(shuffled_locations)
    fill_dungeons_restrictive(world, shuffled_locations)

    logger.info('Fill the world.')

    distribute_items_restrictive(world)

    logger.info('Calculating playthrough.')

    create_playthrough(world)

    logger.info('Patching ROM.')

    outfilebase = 'OoT_%s_%s' % (world.settings_string,  world.seed)
    output_dir = default_output_path(settings.output_dir)

    if not settings.suppress_rom:
        rom = LocalRom(settings)
        patch_rom(world, rom)

        rom_path = os.path.join(output_dir, '%s.z64' % outfilebase)

        rom.write_to_file(rom_path)
        if settings.compress_rom:
            logger.info('Compressing ROM.')
            if platform.system() == 'Windows':
                subprocess.call(["Compress\\Compress.exe", rom_path, os.path.join(output_dir, '%s-comp.z64' % outfilebase)])
            elif platform.system() == 'Linux':
                subprocess.call(["Compress/Compress", rom_path])
            elif platform.system() == 'Darwin':
                subprocess.call(["Compress/Compress.out", rom_path])
            else:
                logger.info('OS not supported for compression')

    if settings.create_spoiler:
        world.spoiler.to_file(os.path.join(output_dir, '%s_Spoiler.txt' % outfilebase))

    logger.info('Done. Enjoy.')
    logger.debug('Total Time: %s', time.clock() - start)

    return world

def copy_world(world):
    # ToDo: Not good yet
    ret = World(world.settings)
    ret.skipped_trials = world.skipped_trials
    ret.seed = world.seed
    ret.can_take_damage = world.can_take_damage
    create_regions(ret)
    create_dungeons(ret)

    # connect copied world
    for region in world.regions:
        copied_region = ret.get_region(region.name)
        for entrance in region.entrances:
            ret.get_entrance(entrance.name).connect(copied_region)

    # fill locations
    for location in world.get_locations():
        if location.item is not None:
            item = Item(location.item.name, location.item.advancement, location.item.priority, location.item.type)
            ret.get_location(location.name).item = item
            item.location = ret.get_location(location.name)
        if location.event:
            ret.get_location(location.name).event = True

    # copy remaining itempool. No item in itempool should have an assigned location
    for item in world.itempool:
        ret.itempool.append(Item(item.name, item.advancement, item.priority, item.type))

    # copy progress items in state
    ret.state.prog_items = list(world.state.prog_items)

    set_rules(ret)

    return ret

def create_spheres(state, locations_to_check):
    sphere_candidates = locations_to_check
    collection_spheres = []
    while sphere_candidates:
        state.sweep_for_events(key_only=True)

        sphere = []
        # build up spheres of collection radius. Everything in each sphere is independent from each other in dependencies and only depends on lower spheres
        for location in sphere_candidates:
            if state.can_reach(location):
                sphere.append(location)

        for location in sphere:
            sphere_candidates.remove(location)
            state.collect(location.item, True, location)

        collection_spheres.append(sphere)

        if not sphere:
            logging.getLogger('').debug('The following items could not be reached: %s', ['%s at %s' % (location.item.name, location.name) for location in sphere_candidates])
            break
    return collection_spheres

def find_spheres(state, locations_to_check):
    locations_to_check_copy = locations_to_check.copy()
    state_copy = state.copy()
    collection_spheres = create_spheres(state_copy, locations_to_check_copy)
    ganonSphereNum = [num for num,hasGanon in list(enumerate([any([location.name == 'Ganon' for location in sphere]) for sphere in collection_spheres])) if hasGanon][0]
    return ganonSphereNum

def create_playthrough(world):
    if world.check_beatable_only and not world.can_beat_game():
        raise RuntimeError('Uncopied is broken too.')
    # create a copy as we will modify it
    old_world = world
    world = copy_world(world)

    # if we only check for beatable, we can do this sanity check first before writing down spheres
    if world.check_beatable_only and not world.can_beat_game():
        raise RuntimeError('Cannot beat game. Something went terribly wrong here!')

    # get locations containing progress items
    prog_locations = [location for location in world.get_filled_locations() if location.item.advancement]
    collection_spheres = []
    state = CollectionState(world)
    min_spheres = find_spheres(state, prog_locations)

    prog_locations_copy = prog_locations.copy()
    for location in prog_locations:
        if location.name == 'Ganon':
            continue
        logging.getLogger('').debug('Checking if %s is required to beat the game.', location.item.name)
        prog_locations_copy.remove(location)
        old_item = location.item
        old_state = state.copy()

        location.item = None
        state.remove(old_item)
        # we remove the item at location and check if game is still beatable in minimal number of spheres
        if (not world.can_beat_game(state)) or (find_spheres(state, prog_locations_copy) != min_spheres):
            state = old_state
            location.item = old_item
            prog_locations_copy.append(location)

    collection_spheres = create_spheres(state, prog_locations_copy)


    # store the required locations for statistical analysis
    old_world.required_locations = [location.name for sphere in collection_spheres for location in sphere]

    def flist_to_iter(node):
        while node:
            value, node = node
            yield value

    def get_path(state, region):
        reversed_path_as_flist = state.path.get(region, (region, None))
        string_path_flat = reversed(list(map(str, flist_to_iter(reversed_path_as_flist))))
        # Now we combine the flat string list into (region, exit) pairs
        pathsiter = iter(string_path_flat)
        pathpairs = zip_longest(pathsiter, pathsiter)
        return list(pathpairs)

    old_world.spoiler.paths = {location.name : get_path(state, location.parent_region) for sphere in collection_spheres for location in sphere}

    # we can finally output our playthrough
    old_world.spoiler.playthrough = OrderedDict([(str(i + 1), {str(location): str(location.item) for location in sphere}) for i, sphere in enumerate(collection_spheres)])
