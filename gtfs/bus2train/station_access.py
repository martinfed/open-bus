from gtfs.parser.gtfs_reader import GTFS, Service
from gtfs.bus2train.utilities import load_train_station_distance, weekdays
from gtfs.parser import route_stories
import datetime
import os
import csv
from collections import defaultdict, Counter

route_types = {0: 'LightRailway', 2: 'Train', 3: 'Bus', 4: 'SharedTaxi'}
day_names = 'sunday monday tuesday wednesday thursday friday saturday'.split()
weekday_names = day_names[:5]
weekend_days = {4, 5}


def format_time(t):
    return '%02d:%02d:%02d' % (t / 3600, t % 3600 / 60, t % 60)


def parse_time(t):
    hour, minute, second = [int(f) for f in t.split(':')]
    return hour * 3600 + minute * 60 + second


class StationAccessFinder:
    """
    ##  Goal

    We want to find
    1) which bus stops are linked to train station (from which stops you can arrive to stations)
    2) What's the travel time from the bus to the station


    ## Implementation

    __1__: find train station bus stops set

    This will be done for now based on straight-line distance.

    Result: set of stop_id objects for near station stops

    __2__: for every route story, find indexes of train station stops

    for each route story, list of stop_sequence values of train station stops. (that is index of train station in the
    list of stops).

    Each time a route passes by a station, only a single stop will count as a station stop.

    Result: dictionary route_story_id -> list of indexes.

    __3__: for each route story that calls at a station, find the time from each stop to the next station

    for each route story, given (1) all stops (2) station stops, calculate the time from the stop to the next train
    station after it.

    + if to_station == False, calculate time from train station to stop

    Result: dictionary route_story_id -> list of (stop_id, station_id, time_to_station)

    __4__: calculate route story frequencies

    done by iterating over trips, similar to current ```route_frequency``` function.

    Result: dictionary route_story_id -> (weekday_frequency, weekend_frequency)

    __5__: load route to route story dictionary

    Result: dictionary route_story_id -> route object

    __6__: aggregate by route

    Route frequency = sum(route_story.frequency for route_story in route.route_stories)

    for each route + stop_id + station_d, calculate time_to_station as weighted average of time_to_station of each
    relevant route story (weighted by route_story frequency)

    Result: list of ```route_id,stop_id,station_id,time_to_station,frequency``` tuples

    __7__: aggregate by stop

    stop.routes = list of route_id for routes that call at stop
    stop.frequency = sum(route.frequency for route in stop.routes)
    stop.time_to_station = sum(route.frequency * route.time_to_station for route in stop.routes) / stop.frequency

    Result:  stop_id,station_id,time_to_station,routes

    (final result, should be dumped)

    # todo:
    # * export stops_near_stations, extended_routes
    # * Better ways to calculate "station stops" (manually using local knowledge? using Google walking directions API?)


    """

    class HasFrequency:
        def __init__(self):
            self.weekday_trips = 0
            self.weekend_trips = 0

        @property
        def total_trips(self):
            return self.weekday_trips + self.weekend_trips

        def add_trip_counts(self, other):
            self.weekday_trips += other.weekday_trips
            self.weekend_trips += other.weekend_trips

    class ExtendedRouteStory(HasFrequency):
        def __init__(self, route_story, station_stops):
            super().__init__()
            self.route_story = route_story
            self.station_stops = station_stops  # stage 2
            self.stop_and_station = []  # stage 3
            self.route = None

    class ExtendedRoute(HasFrequency):
        def __init__(self, route):
            super().__init__()
            self.route = route
            self.stops_to_station_to_travel_time = defaultdict(lambda: 0)

    class StopAndStation(HasFrequency):
        def __init__(self, stop_id, station_id):
            super().__init__()
            self.stop_id = stop_id
            self.station_id = station_id
            self.routes = []
            self.travel_time = 0

    def __init__(self, gtfs_folder, output_folder, start_date, end_date=None, station_stop_distance=300,
                 to_station=True):
        print("StationAccessFinder.__init__")
        self.gtfs = GTFS(os.path.join(gtfs_folder, 'israel-public-transportation.zip'))
        self.gtfs_folder = gtfs_folder
        self.output_folder = output_folder
        if not os.path.exists(self.output_folder):
            os.makedirs(self.output_folder)
        self.to_station = to_station
        # load route stories
        print("Loading route stories")
        self.route_stories, self.trips_to_route_stories = route_stories.load_route_stories_from_csv(
            os.path.join(gtfs_folder, 'route_stories.txt'),
            os.path.join(gtfs_folder, 'trip_to_stories.txt'))
        print("   There are %d route stories" % len(self.route_stories))
        # configuration
        self.station_stop_distance = station_stop_distance
        self.start_date = start_date
        self.end_date = end_date if end_date is not None else start_date + datetime.timedelta(days=7)
        # things that will be build during run()
        self.stops_near_stations = None  # dictionary from stop_id to StopAndDistance object
        self.extended_route_stories = None
        self.extended_routes = None
        self.stop_and_stations = None

    def run_station_access(self):
        # stage 1
        self.find_station_stops()
        # stage 2
        self.route_story_train_station_stops()
        # stage 3
        if self.to_station:
            self.route_story_stops_to_stations()
        else:
            self.stations_to_route_story_stop()
        # stage 4
        self.route_story_frequency()
        # stage 5
        self.route_story_to_route()
        # stage 6
        self.route_stops_and_stations()
        # stage 7
        self.aggregate_by_stop()
        self.export_stop_and_station()
        self.export_readme()

    def export_readme(self):
        with open(os.path.join(self.output_folder, 'readme.txt'), 'w', encoding='utf8') as f:
            f.write("Results of StationAccessFinder\n")
            f.write("Time of execution: %s\n" % datetime.datetime.now())
            f.write("Execution parameters:\n")
            f.write("  gtfs_folder: %s\n" % self.gtfs_folder)
            f.write("  start_date: %s\n" % self.start_date)
            f.write(
                "  bus stop is considered to be serving a train station if it's up to %dm from it (straight line)" %
                self.station_stop_distance)
            f.write('\n\n')
            f.write("Results:\n")
            f.write("  number of bus stops near stations: %d\n" % len(self.stops_near_stations))
            f.write("  number of bus routes calling at stations: %d\n" % len(self.extended_routes))

    def find_station_stops(self, include_trains=False):
        print("Running stage 1: find_station_stops")
        station_distance = load_train_station_distance(self.gtfs_folder)
        near_stations = ((stop_id, station_and_distance) for (stop_id, station_and_distance) in station_distance.items()
                         if station_and_distance.distance < self.station_stop_distance)
        if not include_trains:
            near_stations = ((stop_id, station_and_distance) for (stop_id, station_and_distance) in near_stations
                             if stop_id != station_and_distance.station_id)
        self.stops_near_stations = {stop_id: station_and_distance for (stop_id, station_and_distance) in near_stations}
        print("  %d stops near train stations" % len(self.stops_near_stations))

    def route_story_train_station_stops(self):
        print("Running stage 2: route_story_train_station_stops")
        # route story and all the stops near train stations
        route_story_and_stations = ((route_story,
                                     [stop for stop in route_story.stops if stop.stop_id in self.stops_near_stations])
                                    for route_story in self.route_stories.values())
        # filter only route stories calling at a train station, and create an ExtendedRouteStory object
        extended_route_stories = (self.ExtendedRouteStory(route_story, stops) for (route_story, stops) in
                                  route_story_and_stations if len(stops) > 0)
        # create a dictionary
        self.extended_route_stories = {extended_route_story.route_story.route_story_id: extended_route_story
                                       for extended_route_story in extended_route_stories}
        print("   There %d route_stories calling at train stations" % len(self.extended_route_stories))

    def route_story_stops_to_stations(self):
        print("Running stage 3: route_story_stops_to_stations")
        for extended_route_story in self.extended_route_stories.values():
            station_stops_iter = iter(extended_route_story.station_stops)
            next_station = next(station_stops_iter)
            for route_story_stop in extended_route_story.route_story.stops:
                if route_story_stop.stop_sequence > next_station.stop_sequence:
                    next_station = next(station_stops_iter, None)
                    if next_station is None:
                        break
                assert next_station.arrival_offset >= route_story_stop.arrival_offset, 'sort problem!'
                extended_route_story.stop_and_station.append((route_story_stop, next_station))

    def stations_to_route_story_stop(self):
        print("Running stage 3: stations_to_route_story_stop")
        for extended_route_story in self.extended_route_stories.values():
            station_stops_iter = iter(reversed(extended_route_story.station_stops))
            next_station = next(station_stops_iter)
            for route_story_stop in reversed(extended_route_story.route_story.stops):
                if route_story_stop.stop_sequence < next_station.stop_sequence:
                    next_station = next(station_stops_iter, None)
                    if next_station is None:
                        break
                assert next_station.arrival_offset <= route_story_stop.arrival_offset, 'sort problem!'
                extended_route_story.stop_and_station.append((route_story_stop, next_station))

    def route_story_frequency(self):
        print("Running stage 4: route_story_frequency")
        self.gtfs.load_trips()
        trips = (trip for trip in self.gtfs.trips.values() if
                 trip.service.end_date >= self.start_date or trip.service.start_date <= self.end_date)
        for trip in trips:
            route_story_id = self.trips_to_route_stories[trip.trip_id].route_story.route_story_id
            if route_story_id in self.extended_route_stories:
                extended_route_story = self.extended_route_stories[route_story_id]
                for d in self.date_range():
                    if d.weekday() in weekdays:
                        extended_route_story.weekday_trips += 1
                    else:
                        extended_route_story.weekend_trips += 1

    def route_story_to_route(self):
        print("Running stage 5: route_story_to_route")
        for trip in self.gtfs.trips.values():
            route_story_id = self.trips_to_route_stories[trip.trip_id].route_story.route_story_id
            if route_story_id in self.extended_route_stories:
                self.extended_route_stories[route_story_id].route = trip.route

    def route_stops_and_stations(self):
        print("Running stage 6: route_stops_and_stations")
        self.extended_routes = {}
        for route_story in self.extended_route_stories.values():
            extended_route = self.extended_routes.setdefault(route_story.route.route_id,
                                                             self.ExtendedRoute(route_story.route))
            extended_route.add_trip_counts(route_story)
            for route_story_stop, route_story_station in route_story.stop_and_station:
                station_id = self.stops_near_stations[route_story_station.stop_id].station_id
                key = (route_story_stop.stop_id, station_id)
                travel_time = route_story_station.arrival_offset - route_story_stop.arrival_offset
                extended_route.stops_to_station_to_travel_time[key] += route_story.total_trips * travel_time

        for route in self.extended_routes.values():
            for k in route.stops_to_station_to_travel_time:
                route.stops_to_station_to_travel_time[k] /= route.total_trips

    def aggregate_by_stop(self):
        print("Running stage 7: aggregate_by_stop")
        self.stop_and_stations = {}
        for extended_route in self.extended_routes.values():
            for (stop_id, station_id), travel_time in extended_route.stops_to_station_to_travel_time.items():
                stop_and_station = self.stop_and_stations.setdefault((stop_id, station_id),
                                                                     self.StopAndStation(stop_id, station_id))
                stop_and_station.routes.append(extended_route.route)
                stop_and_station.add_trip_counts(extended_route)
                stop_and_station.travel_time += travel_time * extended_route.total_trips

        for stop_and_station in self.stop_and_stations.values():
            stop_and_station.travel_time /= stop_and_station.total_trips
        print("   %d (stop, station) pairs found" % len(self.stop_and_stations))

    def export_stop_and_station(self):
        print("Running export_stop_and_station")
        self.gtfs.load_stops()
        with open(os.path.join(self.output_folder, 'station_access.txt'), 'w', encoding='utf8') as f:
            writer = csv.DictWriter(f, lineterminator='\n',
                                    fieldnames=['stop_id', 'station_id', 'stop_code', 'station_code',
                                                'travel_time', 'weekday_trips',
                                                'weekend_trips', 'latitude', 'longitude', 'station_name',
                                                'line_numbers', 'route_ids', 'parent_stop'])
            writer.writeheader()
            for stop_and_station in self.stop_and_stations.values():
                writer.writerow({
                    'stop_id': stop_and_station.stop_id,
                    'station_id': stop_and_station.station_id,
                    'stop_code': self.gtfs.stops[stop_and_station.stop_id].stop_code,
                    'station_code': self.gtfs.stops[stop_and_station.station_id].stop_code,
                    'travel_time': int(stop_and_station.travel_time // 60),
                    'weekday_trips': stop_and_station.weekday_trips,
                    'weekend_trips': stop_and_station.weekend_trips,
                    'line_numbers': ' '.join(sorted(set(route.line_number for route in stop_and_station.routes))),
                    'route_ids': ' '.join(str(route.route_id) for route in stop_and_station.routes),
                    'latitude': self.gtfs.stops[stop_and_station.stop_id].stop_lat,
                    'longitude': self.gtfs.stops[stop_and_station.stop_id].stop_lon,
                    'station_name': self.gtfs.stops[stop_and_station.station_id].stop_name,
                    'parent_stop': self.gtfs.stops[stop_and_station.stop_id].parent_station
                })

    def date_range(self):
        d = self.start_date
        while d <= self.end_date:
            yield d
            d += datetime.timedelta(days=1)


def filter_station_access_results(folder, output_filename=None,
                                  max_time_difference_from_station=-1, stations_to_include=None,
                                  stations_to_exclude=None, only_nearest_station=False,
                                  min_weekday_trips=0):
    print("Running filter_station_access_results")
    if output_filename is None:
        output_filename = 'filtered_station_access_%s.txt' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    # read original records and filter them
    with open(os.path.join(folder, 'station_access.txt'), 'r', encoding='utf8') as f:
        reader = csv.DictReader(f)
        records = [r for r in reader]
        original_counter = len(records)
        # filter max_time_difference_from_station
        if max_time_difference_from_station != -1:
            records = (r for r in records if int(r['travel_time']) <= max_time_difference_from_station)
        if stations_to_include:
            records = (r for r in records if int(r['station_id']) in stations_to_include)
        if stations_to_exclude:
            records = (r for r in records if int(r['station_id']) not in stations_to_exclude)
        if only_nearest_station:
            stop_to_record = {}
            for r in records:
                stop_id = r['stop_id']
                if stop_id not in stop_to_record or int(r['travel_time']) < int(stop_to_record[stop_id]['travel_time']):
                    stop_to_record[stop_id] = r
            records = stop_to_record.values()
        records = (r for r in records if int(r['weekday_trips']) >= min_weekday_trips)
    records = list(records)
    filtered_counter = len(records)

    with open(os.path.join(folder, output_filename), 'w', encoding='utf8') as w:
        writer = csv.DictWriter(w, fieldnames=reader.fieldnames, lineterminator='\n')
        writer.writeheader()
        writer.writerows(r for r in records)

    # document output
    readme_filename = os.path.splitext(output_filename)[0] + ".readme.txt"
    with open(os.path.join(folder, readme_filename), 'w', encoding='utf8') as f:
        f.write("Results of filter_station_access_results\n")
        f.write('input_file=%s\n' % os.path.join(folder, 'stops_and_stations.txt'))
        f.write('max_time_difference_from_station=%d\n' % max_time_difference_from_station)
        f.write('stations_to_include=%s\n' % stations_to_include)
        f.write('stations_to_exclude=%s\n' % stations_to_exclude)
        f.write('only_nearest_station=%s\n' % only_nearest_station)
        f.write('min_weekday_trips=%d\n' % min_weekday_trips)
        f.write('Number of original records=%d\n' % original_counter)
        f.write('Number of records after filter=%d\n' % filtered_counter)
    print("Done.")


if __name__ == '__main__':
    gtfs_folder = '../openbus_data/gtfs_2016_05_25'
    # output_folder = 'train_access_map'
    # finder = StationAccessFinder(gtfs_folder, output_folder, datetime.date(2016, 6, 1))
    # finder.run_station_access()
    # busiest_train_stations = {37358, 37312, 37350, 37388, 37292, 37376, 37378, 37318, 37386, 37380, 37348, 37360}
    # filter_station_access_results(output_folder, max_time_difference_from_station=30,
    #                               stations_to_exclude=busiest_train_stations, only_nearest_station=True,
    #                               min_weekday_trips=25)
    output_folder = 'train_access_stats'
    #finder = CallingAtStation(gtfs_folder, output_folder, datetime.date(2016, 6, 1), datetime.date(2016, 6, 14),
    #                          max_station_distance=300)
    #finder.run_calling_at_station()
    #CallingAtStation.explode_stop_data(output_folder)
    #CallingAtStation.explode_stop_data(output_folder, route_type=3)
    #CallingAtStation.explode_stop_data(output_folder, route_type=2)
    finder = StationAccessFinder(gtfs_folder, output_folder, datetime.date(2016, 6, 1), to_station=False)
    finder.run_station_access()
