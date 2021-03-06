import csv
import json
import random
import urllib
import yaml
import itertools

from operator import itemgetter

import requests
import shapefile

from django import http
from django.core import serializers
from django.core.urlresolvers import reverse
from django.db import transaction, connection
from django.db.models import Q
from django.shortcuts import render
from django.views import generic
from django.db.utils import DatabaseError

from multigtfs.models import Agency, Route, Stop, Feed, FeedInfo, Service, Trip, StopTime, Shape, ShapePoint, Ride, NewStop
from django.template.defaultfilters import slugify
from django.core.servers.basehttp import FileWrapper
from datetime import datetime

from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.contrib.gis import geos
from django.core.exceptions import  ObjectDoesNotExist


class DictReaderStrip(csv.DictReader):
    @property
    def fieldnames(self):
        if self._fieldnames is None:
            # Initialize self._fieldnames
            # Note: DictReader is an old-style class, so can't use super()
            csv.DictReader.fieldnames.fget(self)
            if self._fieldnames is not None:
                self._fieldnames = [name.strip() for name in self._fieldnames]
        return self._fieldnames


class FeedListView(generic.ListView):
    template_name = 'myapp/feed-list.html'
    context_object_name = 'feed_list'
    model = Feed
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super(FeedListView, self).get_context_data(**kwargs)
        request_params = self.request.GET.copy()
        if 'page' in request_params:
            del request_params['page']

        request_params = filter(itemgetter(1), request_params.items())

        if request_params:
            context['request_params'] = request_params
        return context


class AgencyListView(generic.ListView):
    template_name = 'myapp/agency-list.html'
    context_object_name = 'agency_list'
    model = Agency
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super(AgencyListView, self).get_context_data(**kwargs)
        request_params = self.request.GET.copy()
        if 'page' in request_params:
            del request_params['page']

        request_params = filter(itemgetter(1), request_params.items())

        if request_params:
            context['request_params'] = request_params
            
        context['feed_id'] = self.kwargs['feed_id']
        return context

    def get_queryset(self):
        return Agency.objects.filter(feed_id=self.kwargs['feed_id'])


class RouteListView(generic.ListView):
    template_name = 'myapp/route-list.html'
    context_object_name = 'route_list'
    model = Route
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super(RouteListView, self).get_context_data(**kwargs)
        context['agency_id'] = self.kwargs['agency_id']
        context['feed_id'] = self.kwargs['feed_id']

        request_params = self.request.GET.copy()
        if 'page' in request_params:
            del request_params['page']

        # request_params = filter(itemgetter(1), request_params.items())

        if request_params:
            context['request_params'] = request_params

        return context

    def get_queryset(self):
        queryset = super(RouteListView, self).get_queryset()
        queryset = queryset.filter(feed_id=self.kwargs['feed_id'], agency_id=self.kwargs['agency_id'])

        q = self.request.GET.get('q')
        if q:
            queryset = queryset.filter(Q(short_name__icontains=q) | (Q(desc__icontains=q)))
        queryset = queryset.order_by('short_name')
        return queryset


class RouteDetailView(generic.DetailView):
    template_name = 'myapp/route-detail.html'
    context_object_name = 'route'
    model = Route

    def get_context_data(self, **kwargs):
        context = super(RouteDetailView, self).get_context_data(**kwargs)
        context['agency_id'] = self.kwargs['agency_id']
        context['feed_id'] = self.kwargs['feed_id']
        context['route_id'] = self.kwargs['pk']
        return context

    def get_queryset(self):
        return Route.objects.filter(pk=self.kwargs['pk'])


def trip_detail_view(request, **kwargs):
    context = dict()

    trip = Trip.objects.get(pk=kwargs['pk'])
    trip.update_geometry()

    corridor_prefix = trip.route.route_id[0].zfill(2)
    inbound_status = trip.direction

    cursor = connection.cursor()
    cursor.execute(
        "SELECT n.id, n.latitude, n.longitude FROM multigtfs_ride r JOIN multigtfs_newstop n ON(n.ride_id=r.id)"
        " WHERE r.route_id = " + kwargs['route_id'])
    columns = [column[0] for column in cursor.description]
    new_stops = []

    for row in cursor.fetchall():
        new_stops.append(dict(zip(columns, row)))
    new_stops = yaml.load(json.dumps(new_stops))

    cursor.execute(
        "SELECT CONCAT(n.longitude, ' ', n.latitude) FROM multigtfs_ride r JOIN multigtfs_newroute n ON(n.ride_id=r.id)"
        " WHERE r.route_id = " + kwargs['route_id'])
    new_stops_route = []

    for row in cursor.fetchall():
        for i in row:
            i = i.encode('latin-1')
            i = i.translate(None, "()#")
            new_stops_route.append(i)
    stops = Stop.objects.filter(parent_station__isnull=True).order_by('name')

    context['agency_id'] = kwargs['agency_id']
    context['feed_id'] = kwargs['feed_id']
    context['route_id'] = kwargs['route_id']
    context['trip'] = trip
    context['stops'] = stops
    context['corridor'] = corridor_prefix
    context['inbound_status'] = inbound_status
    context['new_stops'] = new_stops
    context['new_stops_route'] = new_stops_route

    if request.method == 'POST':
        start_seconds = 6 * 3600  # First trip is at 6am
        delta = 5 * 60  # % minutes

        stop_ids = request.POST.getlist('stop_id')
        try:
            with transaction.atomic():
                # Delete existing stop times
                stop_times = trip.stoptime_set.all()
                for stop_time in stop_times:
                    stop_time.delete()

                # Add new stop times
                for index, stop_id in enumerate(stop_ids):
                    data = {
                        'stop_id': stop_id,
                        'trip_id': trip.id,
                        'stop_sequence': index + 1,
                        'arrival_time': start_seconds,
                        'departure_time': start_seconds
                    }
                    trip.stoptime_set.add(StopTime(**data))
                    start_seconds += delta

                    # Delete existing route shape

                    # trip.shape

                    # Add new route shape
        except DatabaseError as e:
            context['error_message'] = 'An error occurred while processing your request.'

        return http.HttpResponseRedirect(reverse('trip_detail', kwargs=kwargs))
    return render(request, 'myapp/trip-detail.html', context)


def add_stop_ajax(request, **kwargs):
    if request.method == 'POST':
        if request.is_ajax():
            request_params = request.POST.dict()
            print(request_params)
            try:
                valid = False
                stop_id_prefix = request_params.get('stop_id_prefix')
                stop_name = request_params.get('name')
                stop_id_suffix = list(stop_name.replace(' ', '').upper())

                while not valid:
                    random.shuffle(stop_id_suffix)
                    stop_id = stop_id_prefix + "".join(stop_id_suffix[:3])

                    valid = Stop.objects.filter(stop_id=stop_id).count() == 0

                params = {
                    'name': stop_name,
                    'stop_id': stop_id,
                    'point': request_params.get('point'),
                    'feed_id': request_params.get('feed_id')
                }
                stop = Stop(**params)
                stop.save()
                return http.HttpResponse(json.dumps({'id': stop.id,
                                                     'name': stop.name,
                                                     'lat': stop.point.y,
                                                     'lon': stop.point.x}), status=201)
            except DatabaseError as e:
                return http.HttpResponse(status=400, content="A problem occurred. Stop not created")


class ParentStopListJSONView(generic.ListView):
    model = Stop

    def get_queryset(self):
        stations = Stop.objects.filter(feed_id=self.kwargs['feed_id'],
                                       parent_station_id__isnull=False).values_list('parent_station_id',
                                                                                    flat=True).distinct()
        return Stop.objects.filter(id__in=stations).order_by('name')

    def get(self, request, *args, **kwargs):
        return http.HttpResponse(serializers.serialize('json', self.get_queryset()))


class StopListJSONView(generic.ListView):
    model = Stop

    def get_queryset(self):
        if self.request.GET.get('inbound_status') and self.request.GET.get('corridor'):
            inbound_status = self.request.GET.get('inbound_status')
            corridor = self.request.GET.get('corridor')
            regex = r"^{}\d{}".format(corridor, inbound_status)

            queryset = Stop.objects.filter(stop_id__regex=regex, feed_id=self.kwargs['feed_id'])

        return queryset.order_by('name')

    def get(self, request, *args, **kwargs):
        return http.HttpResponse(serializers.serialize('json', self.get_queryset()))


def get_route_ajax(request, **kwargs):
    if request.method == 'GET':
        params = dict()
        params['json'] = urllib.unquote(str(request.GET.get('json')))
        params['api_key'] = urllib.unquote(str(request.GET.get('api_key')))

        locs_json = params['json']
        locs_json = json.loads(locs_json)
        points = ''
        for location in locs_json['locations']:
            points += str(location['lon']) + ',' + str(location['lat']) + ';'

        points = points[:-1]
        # url = 'https://valhalla.mapzen.com/route?api_key={api_key}&json={json}'.format(**params)
        # url1 = 'http://router.project-osrm.org/route/v1/driving/13.388860,52.517037;13.397634,52.529407;13.428555,52.523219?overview=false'
        url = 'http://router.project-osrm.org/route/v1/driving/' + points + '?overview=false'

        response = requests.get(url)

        return http.HttpResponse(status=200, content=response.text)
    return http.HttpResponse(status=400)


def parse_update_params(request_params):
    result = dict()
    pk = request_params['pk']

    del request_params['pk']
    del request_params['csrfmiddlewaretoken']

    if 'name' in request_params and 'value' in request_params:
        result[request_params['name']] = request_params['value']
        del request_params['value']
        del request_params['name']

    result.update(**request_params)
    return pk, result


def _update_ajax(ModelClass, request):
    if request.method == 'POST' and request.is_ajax():
        pk, request_params = parse_update_params(request.POST.dict())
        ModelClass.objects.filter(pk=pk).update(**request_params)
        return ModelClass.objects.get(pk=pk)


def update_route_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            route = _update_ajax(Route, request)
            return http.HttpResponse(json.dumps({
                'pk': route.id,
            }), status=201)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def update_stop_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            stop = _update_ajax(Stop, request)
            return http.HttpResponse(json.dumps({'pk': stop.id,
                                                 'name': stop.name,
                                                 'lat': stop.point.y,
                                                 'lng': stop.point.x}), status=201)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def update_trip_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            trip = _update_ajax(Trip, request)
            return http.HttpResponse(json.dumps({'pk': trip.id}), status=201)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def update_agency_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            trip = _update_ajax(Agency, request)
            return http.HttpResponse(json.dumps({'pk': trip.id}), status=201)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def update_feed_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        pk, request_params = parse_update_params(request.POST.dict())

        try:
            if 'name' in request_params:
                Feed.objects.filter(pk=pk).update(**request_params)

                del request_params['name']

            FeedInfo.objects.filter(feed_id=pk).update(**request_params)
            return http.HttpResponse(status=200)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def delete_stop_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            stop = Stop.objects.get(pk=request.POST.get('pk'))
            stop_name = stop.name

            if stop.stoptime_set.count() > 1:
                return http.HttpResponse(status=400,
                                         content='Stop <strong>{}</strong> is still in use in other Trips'.format(
                                             stop_name))
            else:
                stop.delete()
                return http.HttpResponse(
                    content='Stop <strong>{}</strong> has been successfully deleted'.format(stop_name),
                    status=200)
        except DatabaseError as e:
            print e
            return http.HttpResponse(status=400, content='An error occurred while processing your request')


def delete_route_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            route = Route.objects.get(pk=request.POST.get('pk'))
            route_short_name = route.short_name
            route.delete()
            return http.HttpResponse(
                content='Route <strong>{}</strong> has been successfully deleted'.format(route_short_name),
                status=200)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def delete_trip_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            trip = Trip.objects.get(pk=request.POST.get('pk'))
            trip_direction = trip.headsign
            trip.delete()
            return http.HttpResponse(
                content='Trip <strong>{}</strong> has been successfully deleted'.format(trip_direction),
                status=200)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def delete_feed_ajax(request, **kwargs):
    if request.method == 'POST' and request.is_ajax():
        try:
            feed = Feed.objects.get(pk=request.POST.get('pk'))
            feed_name = feed.name
            feed.delete()
            return http.HttpResponse(
                content='Trip <strong>{}</strong> has been successfully deleted'.format(feed_name),
                status=200)
        except DatabaseError as e:
            return http.HttpResponse(status=400, content='An error occurred while processing your request')
    return http.HttpResponse(status=400)


def new_route(request, **kwargs):
    if request.method == 'POST':
        try:
            # Build route_id
            request_params = request.POST.dict()
            # prepend zeros to the route number
            request_params['route-number'] = request_params['route-number'].zfill(4)

            route_mask = "{corridor}{first-level-branch}{second-level-branch}{route-number}{gazetted}{inbound}"
            params = {
                'route_id': route_mask.format(**request_params),
                'short_name': request_params.get('route-number'),
                'desc': request_params.get('description'),
                'rtype': request_params.get('route-type'),
                'agency_id': kwargs.get('agency_id'),
                'feed_id': kwargs.get('feed_id')
            }

            route = Route(**params)
            route.save()
            route.refresh_from_db()

            # add the newly generated route_id to kwargs
            kwargs['pk'] = route.id

            return http.HttpResponseRedirect(reverse('route_detail', kwargs=kwargs))
        except Exception as e:
            kwargs['alert_type'] = 'alert-danger'
            kwargs['error_message'] = 'An error occurred while processing your request'

    return render(request, 'myapp/new-route.html', kwargs)


def new_trip(request, **kwargs):
    route = Route.objects.get(id=kwargs.get('route_id'))
    # Create route + shape
    context = dict()
    context.update(kwargs)
    context['headsign_options'] = route.desc.split('-')
    context['service_times'] = Service.objects.all()

    if request.method == 'POST':
        request_params = request.POST.dict()

        shape_file = shapefile.Reader(shp=request.FILES['shape-file'], dbf=request.FILES['shape-file-dbf'])
        stops_reader = DictReaderStrip(request.FILES['stops-file'])

        # Check required fields are present in the stops csv
        expected_fields = set(['stop_sequence', 'lat', 'lon', 'stop_name', 'designation', 'location_type'])
        current_fields = set(stops_reader.fieldnames)

        if not expected_fields.issubset(current_fields):
            missing_fields = expected_fields.difference(current_fields)
            context['error_message'] = 'The following columns are missing from the uploaded stops file: {}.'.format(missing_fields)
            return render(request, 'myapp/new-trip.html', context)

        # Trip variables
        headsign = request_params['headsign']
        service_id = request_params['service-id']
        direction = request_params['inbound']
        route_id = kwargs['route_id']

        # corridor + 4 characters for the route number
        corridor = route.route_id[0]
        route_number = route.route_id[5:9]
        origin = request_params['origin']
        route_variation = request_params['route-variation']
        shape_id = "{}{}{}{}{}".format(corridor, route_number, origin, route_variation, direction)

        trip_id = shape_id

        with transaction.atomic():
            # Create new shape
            shape = Shape(
                feed_id=kwargs['feed_id'],
                shape_id=shape_id)
            shape.save()

            trip = Trip(
                trip_id=trip_id,
                headsign=headsign,
                service_id=service_id,
                direction=direction,
                route_id=route_id,
                shape_id=shape.id
            )
            trip.save()

            # Create shape points from the uploaded shape files
            shapes = shape_file.shapes()

            sequence_start = 1001
            # The  trip line string is stored in layer 1
            for idx, point in enumerate(shapes[1].points):
                shape_point = ShapePoint(
                    point='POINT ({} {})'.format(point[0], point[1]),
                    shape_id=shape.id,
                    sequence=sequence_start + idx
                )

                shape_point.save()
            shape.update_geometry()

            start_seconds = 6 * 3600  # First trip is at 6am
            delta = 5 * 60  # % minutes

            for row in stops_reader:
                tmp = list(row['stop_name'].upper().replace(' ', ''))
                random.shuffle(tmp)
                stop_suffix = "".join(tmp[:3])  # pick 3 characters from the shuffled stop name

                stop, created = Stop.objects.get_or_create(
                    point = geos.fromstr('POINT({} {})'.format(row['lon'], row['lat'])),
                    feed_id = kwargs['feed_id'],
                    defaults = {
                        'stop_id': '{}{}{}{}'.format(corridor.zfill(2), row['designation'] or 0, direction, stop_suffix),
                        'name': row['stop_name'],
                        'location_type' : row['location_type'],
                    }
                )

                trip.stoptime_set.add(StopTime(stop_id=stop.id,
                                               trip_id=trip.id,
                                               stop_sequence=int(row['stop_sequence']) + 1,
                                               arrival_time=start_seconds,
                                               departure_time=start_seconds
                                               ))
                start_seconds += delta

            trip.save()
            trip.update_geometry()
            trip.refresh_from_db()

            return http.HttpResponseRedirect(reverse('route_detail', kwargs={
                'pk': kwargs['route_id'],
                'feed_id': kwargs['feed_id'],
                'agency_id': kwargs['agency_id']
            }))

    return render(request, 'myapp/new-trip.html', context)


def update_shape_ajax(request, **kwargs):
    if request.method == 'POST':
        request_params = request.POST.dict()

        shape_id = request_params['id']
        points = json.loads(request_params['points'])

        try:
            with transaction.atomic():
                shape = Shape.objects.get(pk=shape_id)
                shape_points = shape.points.all()
                for shape_point in shape_points:
                    shape_point.delete()

                for idx, point in enumerate(points):
                    shape_point = ShapePoint(
                        point='POINT ({} {})'.format(point['lng'], point['lat']),
                        shape_id=shape.id,
                        sequence=idx + 1
                    )
                    shape_point.save()
                shape.update_geometry()
                return http.HttpResponse(content='Shape updated successfully', status=200)
        except Exception as e:
            return http.HttpResponse(content='An error occurred while processing your request', status=400)


def _delete_unused_stops(feed_id):
    used_stops = StopTime.objects.all().values_list('stop_id', flat=True)

    # Delete any unused stops
    Stop.objects.filter(~Q(location_type='1'), ~Q(id__in=used_stops), feed_id=feed_id).delete()


def export_feed(request, **kwargs):
    feed = Feed.objects.get(id=kwargs['feed_id'])
    file_name = "{}_{}.zip".format(slugify(feed.name), datetime.now().strftime("%Y%m%d-%H%M%S"))
    output_path = "/tmp/{}".format(file_name)
    feed.export_gtfs(output_path)

    temp = file(name=output_path, mode='rb')
    wrapper = FileWrapper(temp)
    response = http.HttpResponse(wrapper,
                                 content_type='application/zip')  # mimetype is replaced by content_type for django 1.7
    response['Content-Disposition'] = 'attachment; filename={}'.format(file_name)
    response['Content-Length'] = temp.tell()
    temp.seek(0)

    return response


@receiver(post_delete, sender=Trip, dispatch_uid='trip_delete_signal')
def _trip_post_delete(sender, instance, using, **kwargs):
    '''
    It seems shapes are not deleted when trips are deleted. This fixes that by listening on the deletion event
    of a trip and then delete the associated shapes
    '''
    try:
        if instance.shape:
            instance.shape.delete()
    except ObjectDoesNotExist as e:
        pass


def new_feed(request, **kwargs):
    error_message = None
    if request.method == 'POST':
        request_data = request.POST.dict()

        try:
            with transaction.atomic():
                feed = Feed.objects.create(name=request_data['feed-name'])
                feed.import_gtfs(request.FILES['feed-file'])
                return http.HttpResponseRedirect(reverse('agency_list', kwargs={'feed_id': feed.id}))
        except:
            error_message = "Something went wrong while processing your upload. Please try again later"
    return render(request, 'myapp/new-feed.html', {'error_message': error_message})


def confirm_stop_ajax(request, **kwargs):
    if request.method == 'POST':
        if request.is_ajax():
            request_params = request.POST.dict()
            try:
                valid = False
                stop_id_prefix = request_params.get('stop_id_prefix')
                stop_name = request_params.get('name')
                stop_id_suffix = list(stop_name.replace(' ', '').upper())

                while not valid:
                    random.shuffle(stop_id_suffix)
                    stop_id = stop_id_prefix + "".join(stop_id_suffix[:3])

                    valid = Stop.objects.filter(stop_id=stop_id).count() == 0

                params = {
                    'name': stop_name,
                    'stop_id': stop_id,
                    'point': request_params.get('point'),
                    'feed_id': request_params.get('feed_id')
                }
                stop = Stop(**params)
                stop.save()
                return http.HttpResponse(json.dumps({'id': stop.id,
                                                     'name': stop.name,
                                                     'lat': stop.point.y,
                                                     'lon': stop.point.x}), status=201)
            except DatabaseError as e:
                return http.HttpResponse(status=400, content="A problem occurred. Stop not created")