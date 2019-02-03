#!/usr/bin/python3

import argparse
import datetime
import functools
import getpass
import json
import logging
import pprint
import re
import time
import urllib.parse

import dateutil
import dateutil.parser
import requests

log = logging.getLogger(__name__)

# reduce logging noise from requests library
logging.getLogger('requests').setLevel(logging.ERROR)

# The Garmin Connect Single-Sign On login URL.
SSO_LOGIN_URL = 'https://sso.garmin.com/sso/login'

# The weight date range url.
WEIGHT_DATE_RANGE_URL = 'https://connect.garmin.com/modern/proxy/weight-service/weight/dateRange'

# The wellness url (username must be formatted at end).
WELLNESS_URL = 'https://connect.garmin.com/modern/proxy/userstats-service/wellness/daily/{}'


def require_session(client_function):
    """Decorator that is used to annotate :class:`GarminClient`
    methods that need an authenticated session before being called.
    """

    @functools.wraps(client_function)
    def check_session(*args, **kwargs):
        client_object = args[0]
        if not client_object.session:
            raise Exception(
                'Attempt to use GarminClient without being connected.')
        return client_function(*args, **kwargs)

    return check_session


class GarminClient(object):
    """A client class used to authenticate with Garmin Connect and
    extract data from the user account.

    Since this class implements the context manager protocol, this object
    can preferably be used together with the with-statement. This will
    automatically take care of logging in to Garmin Connect before any
    further interactions and logging out after the block completes or
    a failure occurs.

    Example of use: ::
      with GarminClient("my.sample@sample.com", "secretpassword") as client:
          ids = client.list_activity_ids()
          for activity_id in ids:
               gpx = client.get_activity_gpx(activity_id)

    """

    def __init__(self, username, password):
        """Initialize a :class:`GarminClient` instance.

        :param username: Garmin Connect user name or email address.
        :type username: str
        :param password: Garmin Connect account password.
        :type password: str
        """
        self.username = username
        self.password = password
        self.session = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()

    def connect(self):
        self.session = requests.Session()
        self._authenticate()

    def disconnect(self):
        if self.session:
            self.session.close()
            self.session = None

    def _authenticate(self):
        log.info('authenticating user ...')
        form_data = {
            'username': self.username,
            'password': self.password,
            'embed': 'false'
        }
        request_params = {'service': 'https://connect.garmin.com/modern'}
        auth_response = self.session.post(
            SSO_LOGIN_URL, params=request_params, data=form_data)
        log.debug('got auth response: %s', auth_response.text)
        if auth_response.status_code != 200:
            raise ValueError(
                'authentication failure: did you enter valid credentials?')
        auth_ticket_url = self._extract_auth_ticket_url(auth_response.text)
        log.debug('auth ticket url: "%s"', auth_ticket_url)

        log.info('claiming auth ticket ...')
        response = self.session.get(auth_ticket_url)
        if response.status_code != 200:
            raise RuntimeError(
                'auth failure: failed to claim auth ticket: %s: %d\n%s' %
                (auth_ticket_url, response.status_code, response.text))

        # appears like we need to touch base with the old API to initiate
        # some form of legacy session. otherwise certain downloads will fail.
        self.session.get('https://connect.garmin.com/legacy/session')

    def _extract_auth_ticket_url(self, auth_response):
        """Extracts an authentication ticket URL from the response of an
        authentication form submission. The auth ticket URL is typically
        of form:

          https://connect.garmin.com/modern?ticket=ST-0123456-aBCDefgh1iJkLmN5opQ9R-cas

        :param auth_response: HTML response from an auth form submission.
        """
        match = re.search(r'response_url\s*=\s*"(https:[^"]+)"', auth_response)
        if not match:
            raise RuntimeError(
                'auth failure: unable to extract auth ticket URL')
        auth_ticket_url = match.group(1).replace('\\', '')
        return auth_ticket_url

    @require_session
    def get_weight(self, start=None, end=None):
        """Return a summary about a given activity. The
        summary contains several statistics, such as duration, GPS starting
        point, GPS end point, elevation gain, max heart rate, max pace, max
        speed, etc).

        :param activity_id: Activity identifier.
        :type activity_id: int
        :returns: The activity summary as a JSON dict.
        :rtype: dict
        """
        if end is None:
            end = datetime.datetime.now()
        if start is None:
            start = end - datetime.timedelta(days=30)
        query_params = {
            '_': int(time.time()),
            'startDate': start.strftime('%Y-%m-%d'),
            'endDate': end.strftime('%Y-%m-%d'),
        }
        response = self.session.get('{}?{}'.format(
            WEIGHT_DATE_RANGE_URL, urllib.parse.urlencode(query_params)))
        if response.status_code != 200:
            log.error('failed to fetch json summary: code={} text={}'.format(
                response.status_code, response.text))
            return None
        return json.loads(response.text)

    @require_session
    def get_calories(self, start=None, end=None):
        query_params = {'_': int(time.time())}
        if start is not None:
            query_params['fromDate'] = start.strftime('%Y-%m-%d')
        if end is not None:
            query_params['untilDate'] = end.strftime('%Y-%m-%d')
        url = '{}?{}'.format(
            WELLNESS_URL.format(self.username),
            urllib.parse.urlencode(query_params))
        for metric in [23, 41, 42]:
            url += '&metricId={}'.format(metric)
        response = self.session.get(url)
        if response.status_code != 200:
            log.error('failed to fetch json summary: code={} text={}'.format(
                response.status_code, response.text))
            return None
        return json.loads(response.text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--start', type=str, help='Start date')
    parser.add_argument('-e', '--end', type=str, help='End date')
    parser.add_argument(
        '-p', '--password', type=str, help='Garmin Connect password')
    parser.add_argument(
        '--pretty-print', action='store_true', help='Pretty print output')
    parser.add_argument(
        '-t',
        '--target',
        choices=['weight', 'calories'],
        default='weight',
        help='Target stat to fetch')
    parser.add_argument('username')
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass('Enter your Garmin Connect password: ')

    start = None
    if args.start:
        start = dateutil.parser.parse(args.start)

    end = None
    if args.end:
        end = dateutil.parser.parse(args.end)

    client = GarminClient(args.username, password)
    client.connect()
    printer = pprint.pprint if args.pretty_print else print

    print(args.target)
    if args.target == 'weight':
        data = client.get_weight(start, end)
    elif args.target == 'calories':
        data = client.get_calories(start, end)
    printer(data)


if __name__ == '__main__':
    main()
