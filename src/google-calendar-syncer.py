import os
import datetime
import dateutil.parser
import logging
import logging.handlers
import json
import argparse
import boto3
import tempfile
import shutil
import binascii
import requests
import re
import pytz
from icalendar import Calendar, vText
from googleapiclient.discovery import build
from httplib2 import Http
from oauth2client import file, client, tools
from bs4 import BeautifulSoup

logging.getLogger('googleapiclient.discovery').setLevel(logging.CRITICAL)
logging.getLogger('oauth2client').setLevel(logging.CRITICAL)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)

# If modifying these scopes, delete the file token.json.
SCOPES = 'https://www.googleapis.com/auth/calendar'


def _get_gcal_event_id(event_id):
    bytes_str = bytes(event_id, 'utf-8')
    hexified = binascii.hexlify(bytes_str)
    gcal_event_id = hexified.decode('utf-8')
    return gcal_event_id


def _get_file_contents_as_json(file_path):
    contents = None
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            contents = json.loads(f.read())
    return contents


def _get_from_s3(s3_client, bucket, key):
    file_contents = None
    # get config from S3
    response = s3_client.get_object(Bucket=bucket, Key=key)
    file_contents = response['Body'].read()
    return file_contents


def _get_from_dynamodb(table_client, key, desired_attrib=None):
    result = None
    # get data from dynamoDB
    response = table_client.get_item(Key={'key': key})
    if 'Item' in response:
        item = response['Item']
        result = item
        if desired_attrib and desired_attrib in item:
            result = item[desired_attrib]
    return result


def _put_file_to_s3(s3_client, bucket, key, file):
    s3_client.upload_file(Filename=file, Bucket=bucket, Key=key)


def _put_obj_to_s3(s3_client, bucket, key, obj):
    obj_contents = bytes(json.dumps(obj).encode('UTF-8'))
    s3_client.put_object(Body=obj_contents, Bucket=bucket, Key=key)


def _put_to_dynamodb(table_client, key, value):
    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
    table_client.put_item(Item={'key': key, 'jsonData': value, 'lastModified': now})


def authorize(path_to_storage):
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    token_path = path_to_storage + os.sep + 'token.json'
    creds_path = path_to_storage + os.sep + 'credentials.json'
    store = file.Storage(token_path)
    # store = file.Storage(token_path)
    creds = store.get()
    if not creds or creds.invalid:
        flow = client.flow_from_clientsecrets(creds_path, SCOPES)
        creds = tools.run_flow(flow, store)
    service = build('calendar', 'v3', cache_discovery=False, http=creds.authorize(Http()))
    return service


def parse_to_string(obj_to_parse):
    result_str = None
    # {'dateTime': '2019-01-11T18:30:16-05:00'} - {'dateTime': '2019-01-11T19:30:16-05:00'}
    if 'dateTime' in obj_to_parse:
        # This is a datetime string like this: 2019-01-11T18:30:16-05:00
        return_date = obj_to_parse['dateTime'].split('T')[0]
        return_time = obj_to_parse['dateTime'].split('T')[1].split('-')[0]
        result_str = f'{return_date} at {return_time}'
    else:
        # Assume date
        result_str = obj_to_parse['date']
    return result_str


def _load_creds_from_s3(s3_client, s3_bucket, storage_path):
    # Grab the oauth creds and cache from S3
    logging.info(f'Using temp dir: {storage_path} for temp storage')
    logging.info('Getting token.json')
    token_contents = _get_from_s3(s3_client, s3_bucket, 'token.json')
    token_path = storage_path + os.sep + 'token.json'
    logging.info(f'Writing token.json to temp dir: {storage_path}')
    with open(token_path, 'wb') as f:
        f.write(token_contents)
    logging.info('Getting credentials.json')
    creds_contents = _get_from_s3(s3_client, s3_bucket, 'credentials.json')
    creds_path = storage_path + os.sep + 'credentials.json'
    logging.info(f'Writing credentials.json to temp dir: {storage_path}')
    with open(creds_path, 'wb') as f:
        f.write(creds_contents)


def _load_creds_from_dynamodb(table_client, storage_path):
    # Grab the oauth creds from DynamoDB
    logging.info(f'Using temp dir: {storage_path} for temp storage')
    logging.info('Getting token.json')
    token_contents = _get_from_dynamodb(table_client, 'token', 'jsonData')
    token_path = storage_path + os.sep + 'token.json'
    logging.info(f'Writing token.json to temp dir: {storage_path}')
    with open(token_path, 'w') as f:
        f.write(token_contents)
    logging.info('Getting credentials.json')
    creds_contents = _get_from_dynamodb(table_client, 'credentials', 'jsonData')
    creds_path = storage_path + os.sep + 'credentials.json'
    logging.info(f'Writing credentials.json to temp dir: {storage_path}')
    with open(creds_path, 'w') as f:
        f.write(creds_contents)


def _get_config_from_s3(s3_client, bucket):
    config = None
    s3_content = _get_from_s3(s3_client, bucket, 'config.json')
    if s3_content:
        config = json.loads(s3_content)
    return config


def _get_config_from_dynamodb(table_client):
    config = None
    dynamodb_content = _get_from_dynamodb(table_client, 'config', 'jsonData')
    if dynamodb_content:
        config = dynamodb_content
    return config


def _load_local_calendar_cache(storage_path):
    # Check to see if we have a cache
    cache = {}
    cache_path = storage_path + os.sep + 'cache'
    for cache_file in os.listdir(cache_path):
        if cache_file.endswith(".old"):
            continue
        else:
            with open(os.path.join(cache_path, cache_file), 'r') as f:
                cal_id = cache_file.rstrip('.cache')
                cache[cal_id] = json.loads(f.read())
    return cache


def _load_dynamodb_calendar_cache(table_client):
    logging.info('Checking for cache...')
    cache_dict = None
    cache_contents = _get_from_dynamodb(table_client, 'cache', 'jsonData')
    if cache_contents:
        logging.info('Cache found')
        cache_dict = json.loads(cache_contents)
    return cache_dict


def _load_s3_calendar_cache(s3_client, s3_bucket):
    logging.info('Checking for cache files...')
    cache_dict = {}
    response = s3_client.list_objects(Bucket=s3_bucket, Prefix='cache/')
    if len(response['Contents']) > 0:
        logging.info('Getting cache files...')
    for obj in response['Contents']:
        s3_path = obj['Key']
        if s3_path == 'cache/':
            continue
        else:
            # This a cache file
            if s3_path.endswith('.old'):
                continue
            cache_obj_contents = _get_from_s3(s3_client, s3_bucket, s3_path).decode('utf-8')
            # Get the cal_id from the s3_path
            cal_id = s3_path.lstrip('cache/')
            cache_dict[cal_id] = json.loads(cache_obj_contents)
    return cache_dict


def _update_local_calendar_cache(storage_path, new_cache, old_cache):
    cache_path = storage_path + os.sep + 'cache'
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)
    for cal in new_cache:
        cache_file_path = cache_path + os.sep + cal + '.cache'
        logging.info(f'Writing cache file to: {cache_file_path}')
        with open(cache_file_path, 'wb') as f:
            f.write(json.dumps(new_cache[cal], indent=4))
    for cal in old_cache:
        cache_file_path = cache_path + os.sep + cal + '.cache.old'
        logging.info(f'Writing cache file to: {cache_file_path}')
        with open(cache_file_path, 'wb') as f:
            f.write(json.dumps(new_cache[cal], indent=4))


def _update_s3_calendar_cache(s3_client, bucket, new_cache, old_cache):
    for cal in new_cache:
        cache_key = 'cache/' + cal + '.cache'
        _put_obj_to_s3(s3_client, bucket, cache_key, new_cache[cal])
    for cal in old_cache:
        old_cache_key = 'cache/' + cal + '.cache.old'
        _put_obj_to_s3(s3_client, bucket, old_cache_key, old_cache[cal])


def _update_dynamodb_calendar_cache(table_client, new_cache=None, old_cache=None):
    if new_cache:
        _put_to_dynamodb(table_client, 'cache', json.dumps(new_cache))
    if old_cache:
        _put_to_dynamodb(table_client, 'cache.old', json.dumps(old_cache))


def _get_last_sync_time_from_dynamodb(table_client):
    logging.info('Checking for last sync time...')
    last_sync = None
    last_sync_contents = _get_from_dynamodb(table_client, 'last_sync', 'lastModified')
    if last_sync_contents:
        last_sync = last_sync_contents
        logging.info(f'Found last sync time: {last_sync}')
    return last_sync


def _get_last_sync_time_from_s3(s3_client, bucket):
    last_sync = None
    s3_content = _get_from_s3(s3_client, bucket, 'last_sync')
    if s3_content:
        last_sync = s3_content
        logging.info(f'Found last sync time: {last_sync}')
    return last_sync


def _update_last_sync_time_in_dynamodb(table_client, sync_time):
    table_client.put_item(Item={'key': 'last_sync', 'lastModified': sync_time})


def _update_last_sync_time_in_s3(s3_client, bucket, sync_time):
    _put_obj_to_s3(s3_client, bucket, 'last_sync', sync_time)


def _should_be_excluded(event, exclusions=None, filters=None):
    result = False
    if exclusions:
        exclude_by_summary_text = exclusions.get('summary')
        event_summary = event.get('summary')
        for exclude_text in exclude_by_summary_text:
            if exclude_text in event_summary:
                event_start = dateutil.parser.parse(event['start']['dateTime'])
                logging.debug(f'Skipping event with start time {event_start} due to exclusion '
                              f'match. Matched "{exclude_text}" in: {event_summary}')
                result = True
                break
    if not result:
        if filters:
            # Currently only supporting filter by summary
            filter_by_summary = filters.get('summary', None)
            if filter_by_summary:
                contains_filter = filter_by_summary.get('contains', [])
                event_summary = event.get('summary')
                found_contains_filter_text = False
                for filter_text in contains_filter:
                    if filter_text in event_summary:
                        # Found the contains filter text in the summary - break out of loop
                        found_contains_filter_text = True
                        break
                if not found_contains_filter_text:
                    logging.debug(f'Skipping event with summary {event_summary} because it does NOT contain '
                                  f'the text in the given contains filter "{contains_filter}"')
                    result = True
    return result


def get_events_for_calendar(starting_datetime, service_client, calendar_id, limit=0):
    all_events = []

    max_results = 250
    if limit != 0:
        max_results = limit

    logging.debug(f'      Getting events from calendar with ID: {calendar_id}')
    try:
        response = service_client.events().list(calendarId=calendar_id, timeMin=starting_datetime,
                                                singleEvents=True, orderBy='startTime', maxResults=max_results).execute()
        events = response.get('items', [])
        all_events.extend(events)

        while 'nextSyncToken' in response:
            logging.debug(f'Getting more events from calendar ID: {calendar_id}')
            response = service_client.events().list(calendarId=calendar_id, timeMin=starting_datetime,
                                                    syncToken=response['nextSyncToken'],
                                                    singleEvents=True, orderBy='startTime',
                                                    maxResults=max_results).execute()
            events = response.get('items', [])
            all_events.extend(events)
    except:
        logging.error(f'Exception fetching events from calendar with ID: {calendar_id}')

    return all_events


def get_ical_calendar(calendar_url):
    req = requests.get(calendar_url)
    if req.status_code != 200:
        logging.error("Error {} fetching {}: {}".format(calendar_url, req.status_code, req.text))
    cal = Calendar.from_ical(req.text)
    return cal


def parse_ics(ical, starting_datetime=None):
    # Default to America/Toronto (EST/EDT)
    past_events = 0
    current_events = 0
    timezone_name = 'America/Toronto'
    for i, comp in enumerate(ical.walk("VTIMEZONE")):
        for name, prop in comp.property_items():
            if name == 'TZID':
                timezone_name = vText.from_ical(str(prop))
                logging.info(f'Found calendar timezone: {timezone_name}')
    events = []
    for i, comp in enumerate(ical.walk()):
        if comp.name == 'VEVENT':
            past_event = False
            event = {}
            for name, prop in comp.property_items():
                if name in ['SUMMARY', 'LOCATION']:
                    event[name.lower()] = prop.to_ical().decode('utf-8').replace('\\,', ',').replace('\\n', '\n')
                elif name == 'DTSTART':
                    tz = pytz.timezone(timezone_name)
                    dt_start = prop.dt.isoformat()
                    event['start'] = {'timeZone': timezone_name}
                    logging.debug(f'Checking event start time {dt_start} against last sync time {starting_datetime}')
                    if 'T' in dt_start:
                        if dateutil.parser.parse(dt_start).replace(tzinfo=tz) < dateutil.parser.parse(starting_datetime):
                            past_event = True
                            break
                        event['start']['dateTime'] = dt_start
                    else:
                        if dateutil.parser.parse(dt_start).replace(tzinfo=tz).date() < dateutil.parser.parse(starting_datetime).date():
                            past_event = True
                            break
                        event['start']['date'] = dt_start
                elif name == 'DTEND':
                    dt_end = prop.dt.isoformat()
                    event['end'] = {'timeZone': timezone_name}
                    if 'T' in dt_end:
                        event['end']['dateTime'] = dt_end
                    else:
                        event['end']['date'] = dt_end
                elif name == 'LAST-MODIFIED':
                    event['last-modified'] = {'dateTime': prop.dt.isoformat()}
                elif name == 'RRULE':
                    event['recurrence'] = [f'RRULE:{prop.to_ical().decode()}']
                elif name == 'DTSTAMP':
                    # logging.info(f'DTSTAMP: {prop.dt.isoformat()}')
                    pass
                elif name == 'SEQUENCE':
                    event[name.lower()] = prop
                elif name == 'UID':
                    # event['id'] = _get_gcal_event_id(prop)
                    event['id'] = prop
                elif name == 'TRANSP':
                    event['transparency'] = prop.lower()
                elif name == 'CLASS':
                    event['visibility'] = prop.lower()
                elif name == 'ORGANIZER':
                    event['organizer'] = {'displayName': prop.params.get('CN') or '',
                                          'email': re.match('mailto:(.*)', prop).group(1) or ''}
                elif name == 'DESCRIPTION':
                    desc = prop.to_ical().decode('utf-8').replace('\\n', '\n')
                    desc = desc.replace(u'\xa0', u' ')
                    if name.lower() in event:
                        event[name.lower()] = desc + '\r\n' + event[name.lower()]
                    else:
                        event[name.lower()] = desc

                elif name == 'X-ALT-DESC' and 'description' not in event:
                    soup = BeautifulSoup(prop, 'lxml')
                    desc = soup.body.text.replace(u'\xa0', u' ').replace('\\n', '\n')
                    if 'description' in event:
                        event['description'] += '\r\n' + desc
                    else:
                        event['description'] = desc

                elif name == 'ATTENDEE':
                    if 'attendees' not in event:
                        event['attendees'] = []
                        event['attendees'].append({'email': re.match('mailto:(.*)', prop).group(1) or ''})
                # VALARM: only remind by UI popup
                elif name == 'ACTION':
                    event['reminders'] = {'useDefault': True}
                elif name == 'BEGIN' or name == 'END':
                    pass
                else:
                    logging.debug(f'skipping unneeded property - {name}: {prop}')
                    pass
            if not past_event:
                logging.debug('Process event')
                current_events += 1
                events.append(event)
            else:
                logging.debug(f'Event occurs in the past - skip it!')
                past_events += 1
    logging.info(f'Skipped {past_events} past events')
    logging.info(f'Found {current_events} future events to process')
    return events


def insert_into_calendar(service_client, event, calendar, adjustments=None, dryrun=False):
    result = True
    new_event_id = _get_gcal_event_id(event['id'])
    new_event = {'id': new_event_id, 'start': event['start'], 'end': event['end'], 'description': event['description']}
    duration_adjust_before = None
    duration_adjust_after = None
    if adjustments:
        duration_adjustment = adjustments.get('duration')
        if duration_adjustment:
            duration_adjust_before = int(duration_adjustment.get('before_event'))
            duration_adjust_after = int(duration_adjustment.get('after_event'))
    duration_adjusted = False
    if 'dateTime' in event['start']:
        event_start = dateutil.parser.parse(event['start']['dateTime'])
        if duration_adjust_before:
            before_adjust_seconds = duration_adjust_before * 60
            event_start = event_start - datetime.timedelta(seconds=before_adjust_seconds)
            logging.info(f'Adjusted start time by {duration_adjust_before} minutes to {event_start}')
        event_end = dateutil.parser.parse(event['end']['dateTime'])
        if duration_adjust_after:
            after_adjust_seconds = duration_adjust_after * 60
            event_end = event_end + datetime.timedelta(seconds=after_adjust_seconds)
            logging.info(f'Adjusted end time by {duration_adjust_after} minutes to {event_end}')
        time_diff = event_end - event_start
        logging.debug(f'Event duration: {time_diff}')
        if time_diff.days < 1:
            if time_diff.seconds < 61:
                # Too short - set new event end time to start + 60 minutes
                logging.warning(f'Event duration: {time_diff}')
                logging.warning(f'Determined event duration to be too short - lengthening to 60 minutes')
                new_event_end = event_start + datetime.timedelta(seconds=3600)
                end_datetime = new_event_end.isoformat()
                new_event['end']['dateTime'] = end_datetime
                duration_adjusted = True
    if 'summary' in event:
        new_event['summary'] = event['summary']
    if 'location' in event:
        new_event['location'] = event['location']
    if 'recurrence' in event:
        new_event['recurrence'] = event['recurrence']
    if 'reminders' in event:
        new_event['reminders'] = event['reminders']
    # TODO: Figure out how to make the creator be "Google Calendar Syncer"
    if not dryrun:
        try:
            logging.info(f'Attempting to insert the following event: {new_event}')
            response = service_client.events().insert(calendarId=calendar, body=new_event).execute()
            logging.info(f"Event created: {response['summary']}")
            logging.info("Date/Time: %s - %s" % (
                parse_to_string(response['start']), parse_to_string(response['end'])))
        except Exception as e:
            logging.warning(f'Exception inserting into calendar: {e}')
            result = False
            if 'The requested identifier already exists' in str(e):
                logging.info('Requested ID already exists - will try updating instead...')
                result = update_event_in_calendar(service_client, event, calendar, None, dryrun)
    else:
        logging.info(f"Dryrun insert event into calendar({calendar}): {new_event}")
    if duration_adjusted:
        logging.info("NOTE: For the above event - the duration was adjusted")
    return result


def delete_from_calendar(service_client, event, calendar, dryrun=False):
    result = True
    gcal_event_id = _get_gcal_event_id(event['id'])
    if not dryrun:
        try:
            response = service_client.events().delete(calendarId=calendar, eventId=gcal_event_id,
                                                      sendUpdates='all').execute()
            logging.debug(f'Response: {response}')
            logging.info(f'Event deleted: {str(event)}')
        except Exception as e:
            logging.error(f'Exception deleting event ({str(event)}) from calendar: {str(e)}')
            result = False

    else:
        logging.info(f"Dryrun delete event from calendar({calendar}): {event['summary']}")
    return result


def update_event_in_calendar(service_client, event, calendar, adjustments=None, dryrun=False):
    result = True
    gcal_event_id = _get_gcal_event_id(event['id'])
    # event_id = event['id'].lstrip('_')
    duration_adjust_before = None
    duration_adjust_after = None
    if adjustments:
        duration_adjustment = adjustments.get('duration')
        if duration_adjustment:
            duration_adjust_before = int(duration_adjustment.get('before_event'))
            duration_adjust_after = int(duration_adjustment.get('after_event'))
    event_start = dateutil.parser.parse(event['start']['dateTime'])
    if duration_adjust_before:
        before_adjust_seconds = duration_adjust_before * 60
        event_start = event_start - datetime.timedelta(seconds=before_adjust_seconds)
        logging.info(f'Adjusted start time by {duration_adjust_before} minutes to {event_start}')
    event_end = dateutil.parser.parse(event['end']['dateTime'])
    if duration_adjust_after:
        after_adjust_seconds = duration_adjust_after * 60
        event_end = event_end + datetime.timedelta(seconds=after_adjust_seconds)
        logging.info(f'Adjusted end time by {duration_adjust_after} minutes to {event_end}')
    updated_event_body = {'start': {'dateTime': event_start.isoformat()}, 'end': {'dateTime': event_end.isoformat()},
                          'description': event['description']}
    if 'summary' in event:
        updated_event_body['summary'] = event['summary']
    if 'location' in event:
        updated_event_body['location'] = event['location']
    if 'recurrence' in event:
        updated_event_body['recurrence'] = event['recurrence']
    if 'reminders' in event:
        updated_event_body['reminders'] = event['reminders']
    # TODO: Figure out how to make the creator be "Google Calendar Syncer"

    if not dryrun:
        try:
            response = service_client.events().update(calendarId=calendar, eventId=gcal_event_id,
                                                      body=updated_event_body, sendUpdates='all').execute()
            logging.info(f"Event updated: {response['summary']}")
            logging.info(
                f"Date(s): %s - %s" % (parse_to_string(response['start']), parse_to_string(response['end'])))
        except Exception as e:
            logging.error(f'Exception updating event ({str(updated_event_body)}) in calendar: {str(e)}')
            result = False
    else:
        logging.info(f"Dryrun update event in calendar({calendar}): {updated_event_body['summary']}")
    return result


def should_be_deleted(event, last_sync_time):
    if 'dateTime' in event['start']:
        tz = pytz.timezone(event['start']['timeZone'])
        start_time = dateutil.parser.parse(event['start']['dateTime']).replace(tzinfo=tz)
        time_diff = start_time - last_sync_time
        if not (time_diff.days < 0):
            # start time of the cached event is in the future, and it doesn't exist anymore - delete it
            return True
    elif 'date' in event['start']:
        # all day event
        start_day = event['start']['date']
        today = datetime.date.today().isoformat()
        if start_day > today:
            # start day of the cached event is in the future, and it doesn't exist anymore - delete it
            return True
    return False


def get_updated_description(event_description, from_cal_name, sync_time, cached_event_description=None):
    # Add synced by message
    logging.debug('Updating event description')
    event_description += f'\n\nSynced from {from_cal_name} by google-calendar-syncer.'
    if cached_event_description:
        if 'First synced on' in cached_event_description:
            # Get the 'first synced on' line and add it to the event description
            match = re.search('First synced on.*', cached_event_description)
            if match:
                first_synced_on = match.group()
                logging.debug('Adding EXISTING First synced on...')
                event_description += f'\n{first_synced_on}'
                logging.debug('Adding Last Syned on...')
                event_description += f'\nLast synced on {sync_time}'
            else:
                logging.debug('Adding NEW First synced on...')
                event_description += f'\nFirst synced on {sync_time}'
        else:
            event_description += f'\nFirst synced on {sync_time}'
    else:
        event_description += f'\nFirst synced on {sync_time}'
    return event_description


def sync_events_to_calendar(service_client, last_sync, from_cal_name, from_cal_cache, from_cal_events, to_cal,
                            limit=0, exclusions=None, filters=None, adjustments=None, dryrun=False):
    events_to_delete = []
    events_to_insert = []
    events_to_update = []
    skipped_due_to_exclusion_match = 0
    if adjustments:
        logging.info(f'Note: Found adjustments for given calendar: {adjustments} -  - all events will be adjusted accordingly')
    date_time_now = datetime.datetime.utcnow().isoformat() + 'Z'
    last_sync_time = dateutil.parser.parse(last_sync)
    if from_cal_cache:
        logging.info(f'Found a cache for the source calendar with name: {from_cal_name}')
        logging.debug(f'Cached Calendar events: {from_cal_cache}')
        logging.info('Comparing cached events against Source calendar events')
        # find events to delete - these will exist in from_cal_cache, but not in from_cal_events
        for cache_event in from_cal_cache:
            # Ignore any exclusion matches
            if _should_be_excluded(cache_event, exclusions, filters):
                skipped_due_to_exclusion_match += 1
            else:
                found_in_from = False
                for from_event in from_cal_events:
                    if _get_gcal_event_id(cache_event['id']) == _get_gcal_event_id(from_event['id']):
                        found_in_from = True
                        break
                if not found_in_from:
                    # This cache_event may need to be deleted
                    if should_be_deleted(cache_event, last_sync_time):
                        logging.info(f"Cache event with ID: {cache_event['id']} should be deleted")
                        events_to_delete.append(cache_event)
        # Now find:
        #    events to insert - these will exist in from_cal_events, but not in cache
        #    events to update - these will exist in both cache and from_cal_events, with different updated times
        for from_event in from_cal_events:
            # Ignore any exclusion matches
            if _should_be_excluded(from_event, exclusions, filters):
                skipped_due_to_exclusion_match += 1
            else:
                found_in_cache = False
                for cache_event in from_cal_cache:
                    if _get_gcal_event_id(cache_event['id']) == _get_gcal_event_id(from_event['id']):
                        # Found it
                        found_in_cache = True
                        # check to see if it's a canceled event
                        if is_canceled_event(cache_event):
                            logging.info('Canceled event - remove from destination calendar')
                            events_to_delete.append(cache_event)
                            break
                        # TODO: Uncomment the code below if we want to be more selective about only updating events that have changed
                        # # now check the updated time
                        # from_event_updated_time = dateutil.parser.parse(from_event['last-modified']['dateTime'])
                        # cache_event_updated_time = dateutil.parser.parse(cache_event['last-modified']['dateTime'])
                        # time_diff = cache_event_updated_time - from_event_updated_time
                        # # if the from_event has a later updated time, we need to update the event
                        # if time_diff.days < 0:
                        #     # Add this to the events_to_update list
                        #     logging.debug(f"Cache event with ID: {cache_event['id']} should be updated")
                        #     events_to_update.append(from_event)
                        logging.debug(f"Cache event with ID: {cache_event['id']} should be updated")
                        event_description = from_event.get('description', '')
                        cached_event_description = cache_event.get('description', '')
                        from_event['description'] = get_updated_description(event_description, from_cal_name,
                                                                            date_time_now, cached_event_description)
                        events_to_update.append(from_event)
                        break
                if not found_in_cache:
                    # Didn't find the event ID in the cached events - it must be new
                    # check to see if it's a canceled event
                    if is_canceled_event(from_event):
                        logging.info('Canceled event - skip')
                    else:
                        logging.debug(f"Calendar event with ID: {from_event['id']} should be inserted")
                        event_description = from_event.get('description', '')
                        from_event['description'] = get_updated_description(event_description, from_cal_name, date_time_now)
                        events_to_insert.append(from_event)
    else:
        # No cache present - need to get events from the destination calendar and compare
        logging.debug('No cache present - need to get events from destination calendar for comparison')
        logging.debug(f'Getting all events for destination calendar with ID: {to_cal}')
        to_calendar_events = get_events_for_calendar(last_sync, service_client, to_cal, limit)
        logging.debug('Destination Calendar events: {}'.format(json.dumps(to_calendar_events)))
        # Need to loop through the from_cal_events and see if we can find a matching one in the to_calendar_events
        # If we can't find it, then we need to insert the event into the to_calendar
        for from_event in from_cal_events:
            if _should_be_excluded(from_event, exclusions, filters):
                skipped_due_to_exclusion_match += 1
            else:
                logging.info(f'Processing event: {from_event["summary"]}')
                event_description = from_event.get('description', '')
                from_event['description'] = get_updated_description(event_description, from_cal_name, date_time_now)
                found = False
                for to_event in to_calendar_events:
                    # check to see if this to_event matches the from_event
                    if _get_gcal_event_id(to_event['id']) == _get_gcal_event_id(from_event['id']):
                        # Found it
                        found = True
                        # check to see if it's a canceled event
                        if is_canceled_event(from_event):
                            logging.info('Canceled event - remove from destination calendar')
                            events_to_delete.append(from_event)
                            break
                        # TODO: Uncomment the code below if we want to be more selective about only updating events that have changed
                        # now check the updated time
                        # from_event_updated_time = dateutil.parser.parse(from_event['last-modified']['dateTime'])
                        # to_event_updated_time = dateutil.parser.parse(to_event['last-modified']['dateTime'])
                        # time_diff = to_event_updated_time - from_event_updated_time
                        # # if the from_event has a later updated time, we need to update the event
                        # if time_diff.days < 0:
                        #     logging.info('Found an event that needs to be updated (based on later updated time)')
                        #     # Need to update this event
                        #     events_to_update.append(from_event)
                        #     break
                        logging.info(f"Event with ID: {from_event['id']} should be updated")
                        events_to_update.append(from_event)
                        break
                if not found:
                    # check to see if it's a canceled event
                    if is_canceled_event(from_event):
                        logging.info('Canceled event - skip')
                    else:
                        events_to_insert.append(from_event)

    if skipped_due_to_exclusion_match > 0:
        logging.info(f'Skipped {skipped_due_to_exclusion_match} events due to exclusion matches')

    if len(events_to_delete) == 0 and len(events_to_insert) == 0 and len(events_to_update) == 0:
        logging.info('No changes found!')
    else:
        if len(events_to_delete) > 0:
            logging.info(f'Found {len(events_to_delete)} events that need to be deleted')
            logging.debug(f'{events_to_delete}')
            failed_deletes = 0
            # Delete any old events
            for old_event in events_to_delete:
                # TODO: Figure out if this event exists in the destination calendar before trying to delete it
                deleted = delete_from_calendar(service_client, old_event, to_cal, dryrun)
                if not deleted:
                    logging.error(f'Failed to delete the following event: {old_event}')
                    failed_deletes += 1

            logging.info(f'Successfully deleted {str(len(events_to_delete) - failed_deletes)} old events from '
                         f'calendar: {to_cal}')
            if failed_deletes > 0:
                logging.info(f'Failed to delete {str(failed_deletes)} old events from calendar: {to_cal}')

        if len(events_to_insert) > 0:
            logging.info(f'Found {len(events_to_insert)} events that need to be added')
            logging.debug(f'{events_to_insert}')
            failed_inserts = 0
            # Insert any new events
            for new_event in events_to_insert:
                inserted = insert_into_calendar(service_client, new_event, to_cal, adjustments, dryrun)
                if not inserted:
                    logging.error(f'Failed to insert the following event: {new_event}')
                    failed_inserts += 1
                    # TODO: How can we handle this so we try to insert it again next time?
                    #       remove it from the cache
            logging.info(f'Successfully added {str(len(events_to_insert) - failed_inserts)} new events to '
                         f'calendar: {to_cal}')
            if failed_inserts > 0:
                logging.info(f'Failed to add {str(failed_inserts)} new events to calendar: {to_cal}')

        if len(events_to_update) > 0:
            logging.info(f'Found {len(events_to_update)} events that need to be updated')
            logging.debug(f'{events_to_update}')
            failed_updates = 0
            # Update events that need updating
            for update_event in events_to_update:
                updated = update_event_in_calendar(service_client, update_event, to_cal, adjustments, dryrun)
                if not updated:
                    logging.error(f'Failed to update the following event {update_event}')
                    failed_updates += 1
                    # TODO: How can we handle this so we try to update it again next time?
                    #       keep it in the cache as is
            logging.info(f'Successfully updated {str(len(events_to_update) - failed_updates)} events in '
                         f'calendar: {to_cal}')
            if failed_updates > 0:
                logging.info(f'Failed to update {str(failed_updates)} events in calendar: {to_cal}')
    return from_cal_events


def is_canceled_event(event):
    description = event.get('description')
    summary = event.get('summary')
    if (description and 'canceled' in description.lower()) or (summary and 'canceled' in summary):
        return True
    return False


def sync_events(service_client, time, config, cache=None, refresh_cache=False, dryrun=False):
    old_cache = {}
    new_cache = {}
    for item in config:
        logging.info(f'Processing {item}')
        dest_cal_id = config[item]['destination_cal_id']
        source_cals = config[item]['source_cals']
        # exclusions = config[item].get('exclusions', None)
        # adjustments = config[item].get('adjustments', None)
        # filters = config[item].get('filters', None)
        for src_cal_type in source_cals:
            for src_cal in source_cals[src_cal_type]:
                if 'ics' in src_cal_type:
                    logging.info(f'Getting events from calendar {src_cal} (ICS)')
                    source_cal_info = source_cals[src_cal_type][src_cal]
                    logging.info(f'Source cal info: {source_cal_info}')
                    src_cal_ics_url = source_cal_info['ics_url']
                    ics_src_cal = get_ical_calendar(src_cal_ics_url)
                    # DEBUG
                    # input_ics_filepath = tempfile.mkdtemp() + 'latest.ics'
                    # with open(input_ics_filepath, 'wb') as ic:
                    #     ic.write(ics_src_cal.to_ical())
                    # logging.info(f'Source ICS file written to: {input_ics_filepath}')
                    logging.debug(f'Source ICS: {ics_src_cal.to_ical()}')
                    src_cal_events = parse_ics(ics_src_cal, time)
                elif 'google' in src_cal_type:
                    logging.info(f'Getting events from calendar {src_cal} (Google)')
                    source_cal_info = source_cals[src_cal_type][src_cal]
                    logging.info(f'Source cal info: {source_cal_info}')
                    src_cal_id = source_cal_info['cal_id']
                    src_cal_events = get_events_for_calendar(time, service_client, src_cal_id, 0)
                else:
                    # Unknown calendar type - exit
                    logging.error(f'Unknown source calendar type {src_cal_type} - unable to process')
                    return None, None
                cal_name = src_cal
                logging.debug(f'Source Calendar ({cal_name}) events: {src_cal_events}')
                if refresh_cache:
                    src_cal_cache = None
                else:
                    src_cal_cache = (cache[cal_name] if cache and cal_name in cache else None)
                exclusions = source_cal_info.get('exclusions', None)
                adjustments = source_cal_info.get('adjustments', None)
                filters = source_cal_info.get('filters', None)
                updated_cache_events = sync_events_to_calendar(service_client,
                                                               time,
                                                               src_cal,
                                                               src_cal_cache,
                                                               src_cal_events,
                                                               dest_cal_id,
                                                               0,
                                                               exclusions,
                                                               filters,
                                                               adjustments,
                                                               dryrun)
                old_cache[cal_name] = (cache[cal_name] if cache and cal_name in cache else None)
                # Remove any canceled events from src_cal_event before writing to cache
                updated_cache_events = [x for x in updated_cache_events if not is_canceled_event(x)]
                new_cache[cal_name] = updated_cache_events
    return old_cache, new_cache


def lambda_handler(event, context):
    log_level = logging.INFO
    debug = event.get('debug', False)
    if debug:
        log_level = logging.DEBUG
    if 'DEBUG' in os.environ and os.environ['DEBUG'].lower() == "true":
        log_level = logging.DEBUG

    logger = logging.getLogger()
    logger.setLevel(log_level)

    logging.debug("Received event: {}".format(json.dumps(event)))

    s3_client = None
    table_client = None

    if 'DYNAMODB_TABLE' in os.environ:
        # Prefer DynamoDB over S3
        table = os.environ.get('DYNAMODB_TABLE')
        dynamodb_client = boto3.resource('dynamodb')
        table_client = dynamodb_client.Table(table)
    elif 'S3_BUCKET' in os.environ:
        bucket = os.environ.get('S3_BUCKET')
        s3_client = boto3.client('s3')
    else:
        logging.critical('Missing required env var (DYNAMODB_TABLE/S3_BUCKET) - cannot continue')
        exit(1)

    dryrun = event.get('dryrun', False)
    if 'DRYRUN' in os.environ:
        dryrun = True

    if dryrun:
        logging.info('Dry run specified - no changes will be made')

    config = None
    if table_client:
        config = _get_config_from_dynamodb(table_client)
    elif s3_client:
        config = _get_config_from_s3(s3_client, bucket)

    if not config:
        logging.critical('Missing config - cannot continue')
        exit(1)

    storage_path = tempfile.mkdtemp()
    # Get the OAUTH credentials
    if table_client:
        _load_creds_from_dynamodb(table_client, storage_path)
        cache = _load_dynamodb_calendar_cache(table_client)
        last_sync_time = _get_last_sync_time_from_dynamodb(table_client)
    else:
        _load_creds_from_s3(s3_client, bucket, storage_path)
        cache = _load_s3_calendar_cache(s3_client, bucket)
        last_sync_time = _get_last_sync_time_from_s3(s3_client, bucket)

    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time

    if not last_sync_time:
        # no last sync time can be found - use the time NOW - only look at event from this point forward
        last_sync_time = now

    service_client = authorize(storage_path)

    cache_refresh = event.get('cache_refresh', False)
    logging.debug("STARTING RUN")
    old_cache, new_cache = sync_events(service_client, last_sync_time, config, cache, refresh_cache=cache_refresh,
                                       dryrun=dryrun)

    if not dryrun:
        # Update the cache
        if table_client:
            _update_dynamodb_calendar_cache(table_client, new_cache, old_cache)
            _update_last_sync_time_in_dynamodb(table_client, now)
        else:
            _update_s3_calendar_cache(s3_client, bucket, new_cache, old_cache)
            _update_last_sync_time_in_s3(s3_client, bucket, now)

    # Update the token file
    token_key = 'token.json'
    token_path = storage_path + os.sep + token_key
    if table_client:
        token_json = _get_file_contents_as_json(token_path)
        _put_to_dynamodb(table_client, 'token', json.dumps(token_json))
    else:
        _put_file_to_s3(s3_client, bucket, token_key, token_path)

    logging.info('Cleaning up...')
    shutil.rmtree(storage_path)
    logging.info("DONE")
    return True


if __name__ == "__main__":
    LOG_FILENAME = 'google-calendar-syncer.log'

    parser = argparse.ArgumentParser(description='google-calendar-syncer')
    parser.add_argument("--init", help="Initialize - create token.json and credentials.json", action='store_true')
    parser.add_argument("--config", help="path to config", dest='config')
    parser.add_argument("--src-cal-id", help="source cal ID", dest='src_cal_id')
    parser.add_argument("--dst-cal-id", help="destination cal ID", dest='dst_cal_id')
    parser.add_argument("--limit", help="Limit to next X events (0)", dest='limit', default=0)
    parser.add_argument("--profile", help="AWS Profile to use when communicating with S3", dest='profile')
    parser.add_argument("--region", help="AWS region S3 bucket is in", dest='region', required=True)
    parser.add_argument("--cleanup", help="Clean up temp folder after execution", action='store_true')
    parser.add_argument("--verbose", help="Turn on DEBUG logging", action='store_true')
    parser.add_argument("--dryrun", help="Do a dryrun - no changes will be performed", dest='dryrun',
                        action='store_true', default=False)
    args = parser.parse_args()

    log_level = logging.INFO

    if args.verbose:
        print("Verbose logging selected")
        log_level = logging.DEBUG

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    # create file handler which logs even debug messages
    fh = logging.handlers.RotatingFileHandler(LOG_FILENAME, maxBytes=5242880, backupCount=5)
    fh.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)8s: %(message)s')
    fh.setFormatter(file_formatter)
    logger.addHandler(fh)
    # create console handler using level set in log_level
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    console_formatter = logging.Formatter('%(levelname)8s: %(message)s')
    ch.setFormatter(console_formatter)
    logger.addHandler(ch)

    # TODO: Figure out why this isn't working
    if args.init:
        # Initialize - just create the token and credentials files
        store = file.Storage('token.json')
        creds = store.get()
        if not creds or creds.invalid:
            flow = client.flow_from_clientsecrets('credentials.json', SCOPES)
            creds = tools.run_flow(flow, store)
        service = build('calendar', 'v3', http=creds.authorize(Http()))
        exit(0)

    if not args.config:
        if not args.src_cal_id or not args.dst_cal_id:
            logging.critical('Must provide either (--src-cal AND --dst-cal) OR --config')
            exit(1)

    config = None
    s3_client = None
    s3_bucket = None
    table_client = None
    dynamodb_client = None

    # Used for token.json, credentials.json and cache folder and files
    storage_path = '.'
    cache = None
    last_sync_time = None

    if args.config:
        if args.config.startswith('s3://'):
            # S3 config
            logging.info('S3 config specified')
            if args.profile or args.region:
                session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
                s3_client = session.client('s3')
            else:
                s3_client = boto3.client('s3')
            s3_path = args.config.split('s3://')[1]
            s3_bucket = s3_path.split('/')[0]
            config = _get_config_from_s3(s3_client, s3_bucket)
            storage_path = tempfile.mkdtemp()
            # Get the credentials
            _load_creds_from_s3(s3_client, s3_bucket, storage_path)
            # Get the cache
            cache = _load_s3_calendar_cache(s3_client, s3_bucket)
            last_sync_time = _get_last_sync_time_from_s3(s3_client, s3_bucket)
        elif args.config.startswith('dynamodb:'):
            # DynamoDB config
            logging.info('DynamoDB config specified')
            table_name = args.config.split('dynamodb:')[1]
            if args.profile or args.region:
                boto3.setup_default_session(profile_name=args.profile)
                dynamodb_client = boto3.resource('dynamodb', region_name=args.region)
            else:
                dynamodb_client = boto3.client('dynamodb')
            table_client = dynamodb_client.Table(table_name)
            config = _get_config_from_dynamodb(table_client)
            storage_path = tempfile.mkdtemp()
            # Get the credentials
            _load_creds_from_dynamodb(table_client, storage_path)
            # Get the cache
            cache = _load_dynamodb_calendar_cache(table_client)
            logging.debug('Cache: {}'.format(json.dumps(cache)))
            last_sync_time = _get_last_sync_time_from_dynamodb(table_client)
        else:
            # Local config file
            logging.info('Local config specified')
            if os.path.exists(args.config):
                with open(args.config, 'r') as f:
                    config = json.loads(f.read())
            else:
                logging.error(f"Config file doesn't exist at given path: {args.config}")
                exit(1)
            cache_path = os.path.join(storage_path, 'cache')
            if os.path.exists(cache_path):
                cache = _load_local_calendar_cache(cache_path)

    else:
        # Single source and destination calendar provided
        config = {
            "Destination Calendar": {
                "destination_cal_id": args.dst_cal_id,
                "source_cals": [
                    {
                        "name": "Source Calendar",
                        "cal_id": args.src_cal_id
                    }
                ]
            }
        }

    if not config:
        logging.critical('Config is empty - cannot continue')
        exit(1)

    logging.debug("STARTING RUN")

    service = authorize(storage_path)

    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time

    if not last_sync_time:
        # no last sync time can be found - use the time NOW - only look at event from this point forward
        last_sync_time = now

    old_cache, new_cache = sync_events(service, last_sync_time, config, cache, dryrun=args.dryrun)

    if not args.dryrun:
        # Update the cache
        if args.config.startswith('s3://'):
            _update_s3_calendar_cache(s3_client, s3_bucket, new_cache, old_cache)
            _update_last_sync_time_in_s3(s3_client, s3_bucket, now)
        if args.config.startswith('dynamodb:'):
            _update_dynamodb_calendar_cache(table_client, new_cache, old_cache)
            _update_last_sync_time_in_dynamodb(table_client, now)
        else:
            _update_local_calendar_cache(storage_path, new_cache, old_cache)
    else:
        logging.info('DRYRUN - cache contents: {}'.format(json.dumps(new_cache)))

    if args.cleanup:
        logging.info('Cleaning up...')
        shutil.rmtree(storage_path)

    logging.info("DONE")
