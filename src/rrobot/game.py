# -*- coding: utf-8 -*-
import argparse
from collections import defaultdict
import math
import asyncio
import logging
import random
import sys
from rrobot.settings import settings
from rrobot.maths import seg_intersect, is_in_angle, get_inverse_square


# TODO: Add power. Attacks, acceleration, and maintaining speed should cost power


logger = logging.getLogger(__name__)


class Game(object):
    def __init__(self, robot_classes):
        """
        Accepts an iterable of Robot classes, and initialises a battlefield
        with them.
        """
        self._turn = 0  # Turn is used to detect stalemate
        self._robots = []  # List of robots in the game
        x_max, y_max = settings['battlefield_size']
        for robot_id, Robot in enumerate(robot_classes):
            x_rand, y_rand = random.randrange(0, x_max), random.randrange(0, y_max)
            self._robots.append({
                'instance': Robot(self, robot_id),
                'coords': (float(x_rand), float(y_rand)),
                'speed': 0.0,
                'damage': 0,
                'heading': 0.0,
                'then': None  # Timestamp of last move
            })

    def _get_robot_attr(self, robot_id, attr):
        return self._robots[robot_id][attr]

    def _set_robot_attr(self, robot_id, attr, value):
        logger.info('{robot} {attr} = {value!r}'.format(
            robot=self._robots[robot_id]['instance'],
            attr=attr,
            value=value))
        self._robots[robot_id][attr] = value

    def get_coords(self, robot_id):
        return self._get_robot_attr(robot_id, 'coords')

    def get_damage(self, robot_id):
        return self._get_robot_attr(robot_id, 'damage')

    def get_heading(self, robot_id):
        return self._get_robot_attr(robot_id, 'heading')

    def set_heading(self, robot_id, rads):
        self._set_robot_attr(robot_id, 'heading', rads)
        logger.debug('({}°)'.format(int(math.degrees(rads))))

    def get_speed(self, robot_id):
        return self._get_robot_attr(robot_id, 'speed')

    def set_speed(self, robot_id, mps):
        mps = min(mps, settings['max_speed'])
        self._set_robot_attr(robot_id, 'speed', mps)

    def attack(self, robot_id):
        """
        Attacking is modelled on a Claymore. Damage is determined by the
        `inverse square`_ of the distance.


        .. _inverse square: http://en.wikipedia.org/wiki/Inverse-square_law
        """
        attacker = self._robots[robot_id]
        logger.info('{robot} attack'.format(robot=attacker['instance']))

        for target in self.active_robots():
            if is_in_angle(attacker['coords'],
                           attacker['heading'],
                           settings['attack_angle'],
                           target['coords']):
                # TODO: Make sure distance between robots is reasonable
                if attacker['coords'] == target['coords']:
                    damage = settings['attack_damage']
                else:
                    damage = int(min(settings['attack_damage'],
                                     get_inverse_square(attacker['coords'],
                                                        target['coords'],
                                                        settings['attack_damage'])))
                if damage:
                    logger.info('{robot} suffered {damage} damage'.format(
                        robot=target['instance'],
                        damage=damage))
                    target['damage'] += damage
                    target['instance'].attacked().send(attacker['instance'].__class__.__name__)

    def active_robots(self):
        """
        Returns a set of robot IDs with damage < 100%
        """
        return [r for r in self._robots if r['damage'] < 100]

    @staticmethod
    def _get_line_seg(data, now):
        """
        Calculates the line segment representing a move based on the given
        robot data and the current time.

        >>> data = {
        ...     'coords': (2, 2),
        ...     'then': 1,
        ...     'speed': 5,
        ...     'heading': 0
        ... }
        >>> Game._get_line_seg(data, 2)
        ((2, 2), (7.0, 2.0))

        """
        # Calc new position
        x, y = data['coords']
        t_d = now - data['then']
        dist = data['speed'] * t_d  # speed is m/s; TODO: Convert t_d to seconds
        x_d = math.cos(data['heading']) * dist
        y_d = math.sin(data['heading']) * dist
        x_new, y_new = x + x_d, y + y_d
        return (x, y), (x_new, y_new)

    @asyncio.coroutine
    def _update_radar(self, robots):
        radar = [{'name': r['instance'].__class__.__name__, 'coords': r['coords']} for r in self._robots]
        logger.info('Radar: %s', radar)
        for robot in robots:
            logger.info('{robot} radar updated'.format(robot=robot['instance']))
            robot['instance'].radar_updated().send(radar)

    @staticmethod
    def _get_intersections(line_segs):
        """
        Return the intersections line segments.

        >>> line_segs = {
        ...     'foo': ((1, 1), (5, 5)),
        ...     'bar': ((1, 3), (3, 1)),
        ...     'baz': ((2, 4), (4, 2))
        ... }
        >>> Game._get_intersections(line_segs) == {
        ...     'foo': {'bar': (2.0, 2.0)},
        ...     'bar': {'foo': (2.0, 2.0)}
        ... }  # doctest: +SKIP
        True
        >>> Game._get_intersections(line_segs) == {
        ...     'foo': {'bar': (2.0, 2.0),
        ...             'baz': (3.0, 3.0)},
        ...     'bar': {'foo': (2.0, 2.0)},
        ...     'baz': {'foo': (3.0, 3.0)}
        ... }
        True

        """
        # Check intersections of each line segment with each other
        intersections = defaultdict(dict)
        for a_id, (a1, a2) in line_segs.items():
            for b_id, (b1, b2) in line_segs.items():
                if a_id == b_id:
                    continue
                intersection = seg_intersect(a1, a2, b1, b2)
                if intersection:
                    intersections[a_id][b_id] = intersection
        # TODO: Find where line segments intersect multiple times, and choose the closest intersection
        return intersections

    @asyncio.coroutine
    def _move_robots(self, robots):
        # Calculate moves as line segments
        loop = asyncio.get_event_loop()
        now = loop.time()
        x_max, y_max = settings['battlefield_size']
        line_segs = {
            'left': [(0, 0), (0, y_max)],
            'top': [(0, y_max), (x_max, y_max)],
            'right': [(x_max, y_max), (x_max, 0)],
            'bottom': [(x_max, 0), (0, 0)]
        }
        for robot in robots:
            line_segs[robot['instance'].id] = list(self._get_line_seg(robot, now))
        # Calculate bumps as intersections of line segments
        bumps = self._get_intersections(line_segs)
        for (a_id, b_id), coords in bumps.items():
            if isinstance(a_id, str):
                # a_id is a border
                continue
            # Set the "to" point to the intersection
            # TODO: Do not put robots on top of each other
            line_segs[a_id][1] = coords
        # Move robots to "to" points
        for robot in robots:
            robot.update({
                'coords': line_segs[robot['instance'].id][1],
                'then': now
            })
        # Stop bumped robots and notify them
        for (a_id, b_id) in bumps.keys():
            if isinstance(a_id, str):
                # a_id is a border
                continue
            if isinstance(b_id, str):
                bumper = b_id
                logger.info('{robot} bumped {bumper} boundary'.format(
                    robot=self._robots[a_id]['instance'],
                    bumper=bumper))
            else:
                bumper = self._robots[b_id]['instance'].__class__.__name__
                logger.info('{robot} bumped {bumper}'.format(
                    robot=self._robots[a_id]['instance'],
                    bumper=self._robots[b_id]['instance']))
            self.set_speed(a_id, 0)
            self._robots[a_id]['instance'].bumped().send(bumper)

    @asyncio.coroutine
    def run_robots(self):
        loop = asyncio.get_event_loop()
        now = loop.time()
        for robot in self._robots:
            logger.info('{robot} started at {coords}'.format(
                robot=robot['instance'],
                coords=robot['coords']))
            robot['instance'].started().send(robot['coords'])
            robot['then'] = now

        robots = self.active_robots()
        while len(robots) > 1 and self._turn < settings['max_turns']:
            self._turn += 1
            logger.info('----------------------------------------')
            logger.info('Turn: %s', self._turn)
            logger.info('Time: %s', loop.time())
            yield from self._update_radar(robots)
            yield from self._move_robots(robots)
            yield from asyncio.sleep(settings['radar_interval'] / 1000)

    def run(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.run_robots())

        winners = ["{} (damage {})".format(r['instance'].__class__.__name__, r['damage'])
                   for r in self.active_robots()]
        return winners


def import_robots(robot_names):
    classes = []
    for robot_name in robot_names:
        module_name, class_name = robot_name.rsplit('.', 1)
        try:
            module = __import__(module_name, globals(), locals(), class_name, 0)
            class_ = getattr(module, class_name)
        except ImportError:
            logger.error('Unable to import "{}". Skipping robot.'.format(robot_name))
            continue
        classes.append(class_)
    return classes


def main(parser_args):
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.setLevel(settings['log_level'])
    logger.info('Robots: {}'.format(parser_args.robot_names))
    robot_classes = import_robots(parser_args.robot_names)
    game = Game(robot_classes)
    winners = game.run()
    if len(winners) > 1:
        print('Stalemate. The survivors are ' + ', '.join(winners))
    elif len(winners) == 1:
        print('The winner is ' + winners[0])
    else:
        print('All robots were destroyed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('robot_names', nargs='+', help='names of robot classes')
    args = parser.parse_args()
    main(args)
