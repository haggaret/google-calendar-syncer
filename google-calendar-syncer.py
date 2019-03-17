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
from googleapiclient.discovery import build
from httplib2 import Http
from oauth2client import file, client, tools

logging.getLogger('googleapiclient.discovery').setLevel(logging.CRITICAL)
logging.getLogger('oauth2client').setLevel(logging.CRITICAL)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)

# If modifying these scopes, delete the file token.json.
SCOPES = 'https://www.googleapis.com/auth/calendar'


def _get_from_s3(s3_client, bucket, key):
    file_contents = None
    # get config from S3
    response = s3_client.get_object(Bucket=bucket,Key=key)
    file_contents = response['Body'].read()
    return file_contents


def _put_to_s3(s3_client, bucket, key, file):
    s3_client.upload_file(Filename=file, Bucket=bucket, Key=key)


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
        result_str = '%s at %s' % (return_date, return_time)
    else:
        # Assume date
        result_str = obj_to_parse['date']
    return result_str


def parse_to_string(obj_to_parse):
    result_str = None
    # {'dateTime': '2019-01-11T18:30:16-05:00'} - {'dateTime': '2019-01-11T19:30:16-05:00'}
    if 'dateTime' in obj_to_parse:
        # This is a datetime string like this: 2019-01-11T18:30:16-05:00
        return_date = obj_to_parse['dateTime'].split('T')[0]
        return_time = obj_to_parse['dateTime'].split('T')[1].split('-')[0]
        result_str = '%s at %s' % (return_date, return_time)
    else:
        # Assume date
        result_str = obj_to_parse['date']
    return result_str


def _load_creds_and_cache(s3_client, s3_bucket, storage_path):
    # Grab the oauth creds and cache from S3
    logging.info('Using temp dir: %s for temp storage' % storage_path)
    logging.info('Getting token.json')
    token_contents = _get_from_s3(s3_client, s3_bucket, 'token.json')
    token_path = storage_path + os.sep + 'token.json'
    logging.info('Writing token.json to temp dir: %s' % storage_path)
    with open(token_path, 'wb') as f:
        f.write(token_contents)
    logging.info('Getting credentials.json')
    creds_contents = _get_from_s3(s3_client, s3_bucket, 'credentials.json')
    creds_path = storage_path + os.sep + 'credentials.json'
    logging.info('Writing credentials.json to temp dir: %s' % storage_path)
    with open(creds_path, 'wb') as f:
        f.write(creds_contents)
    logging.info('Checking for cache files...')
    response = s3_client.list_objects(Bucket=s3_bucket, Prefix='cache/')
    cache_path = storage_path + os.sep + 'cache'
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)
    if len(response['Contents']) > 0:
        logging.info('Getting cache files...')
    for obj in response['Contents']:
        if obj['Key'] == 'cache/':
            continue
        else:
            # This a cache file
            if obj['Key'].endswith('.old'):
                continue
            cache_obj_path = storage_path + os.sep + obj['Key']
            cache_obj_contents = _get_from_s3(s3_client, s3_bucket, obj['Key'])
            logging.info('Writing cache file to: %s' % cache_obj_path)
            with open(cache_obj_path, 'wb') as f:
                f.write(cache_obj_contents)


def _get_config_from_s3(s3_client, bucket, key):
    config = None
    s3_content = _get_from_s3(s3_client, bucket, key)
    if s3_content:
        config = json.loads(s3_content)
    return config


def _get_calendar_cache(storage_path, calendar):
    # Check to see if we have a cache for the from_calendar
    cache = None
    cache_path = storage_path + os.sep + 'cache'
    cache_file = cache_path + os.sep + calendar + '.cache'
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            cache = json.loads(f.read())
    return cache


def _update_calendar_cache(storage_path, calendar, events):
    cache_path = storage_path + os.sep + 'cache'
    cache_file = cache_path + os.sep + calendar + '.cache'
    old_cache_file = cache_file + '.old'
    if os.path.exists(cache_file):
        os.rename(cache_file, old_cache_file)
    if not os.path.exists(cache_path):
        os.mkdir(cache_path)
    with open(cache_file, 'w') as f:
        f.write(json.dumps(events, indent=4))


def _update_s3_calendar_cache(s3_client, bucket, storage_path, calendar):
    cache_key = 'cache/' + calendar + '.cache'
    cache_path = storage_path + os.sep + 'cache'
    new_cache_file = cache_path + os.sep + calendar + '.cache'
    old_cache_file = new_cache_file + '.old'
    old_cache_key = cache_key + '.old'
    _put_to_s3(s3_client, bucket, old_cache_key, old_cache_file)
    _put_to_s3(s3_client, bucket, cache_key, new_cache_file)


def get_events_for_calendar(starting_datetime, service_client, calendar_id, limit=0):
    all_events = []

    max_results = 250
    if limit != 0:
        max_results = limit

    logging.debug('      Getting events from calendar with ID: %s ' % calendar_id)
    response = service_client.events().list(calendarId=calendar_id, timeMin=starting_datetime,
                                     singleEvents=True, orderBy='startTime', maxResults=max_results).execute()
    events = response.get('items', [])
    all_events.extend(events)

    while 'nextSyncToken' in response:
        logging.debug('Getting more events from calendar ID: %s ' % calendar_id)
        response = service_client.events().list(calendarId=calendar_id, timeMin=starting_datetime, syncToken=response['nextSyncToken'],
                                         singleEvents=True, orderBy='startTime', maxResults=max_results).execute()
        events = response.get('items', [])
        all_events.extend(events)

    return all_events


def insert_into_calendar(service_client, event, calendar, dryrun=False):
    new_event = {}

    new_event['id'] = event['id'].lstrip('_')

    new_event['start'] = event['start']
    new_event['end'] = event['end']
    if 'dateTime' in event['start']:
        event_start = dateutil.parser.parse(event['start']['dateTime'])
        event_end = dateutil.parser.parse(event['end']['dateTime'])
        time_diff = event_end - event_start
        if time_diff.seconds < 61:
            # Too short - set new event end time to start + 60 minutes
            new_event_end = event_start + datetime.timedelta(0,3600)
            end_datetime = new_event_end.isoformat()
            new_event['end']['dateTime'] = end_datetime
    if 'summary' in event:
        new_event['summary'] = event['summary']
    if 'location' in event:
        new_event['location'] = event['location']
    if 'description' in event:
        new_event['description'] = event['description']
        new_event['description'] = new_event['description'] + '\n\nSynced by google-calendar-syncer'
    if 'recurrence' in event:
        new_event['recurrence'] = event['recurrence']
    if 'reminders' in event:
        new_event['reminders'] = event['reminders']
    if not dryrun:
        try:
            response = service_client.events().insert(calendarId=calendar, body=new_event).execute()
            logging.info('         Event created: %s' % response['summary'])
            logging.info("             Date/Time: %s - %s" % (parse_to_string(response['start']), parse_to_string(response['end'])))
        except Exception as e:
            logging.warning('         Exception inserting into calendar')
            if 'The requested identifier already exists' in str(e):
                logging.info('         Requested ID already exists - try updating instead...')
                update_event_in_calendar(service_client, event, calendar, dryrun)
    else:
        logging.info('         Dryrun insert event into calendar(%s): %s' % (calendar, new_event['summary']))


def delete_from_calendar(service_client, event, calendar, dryrun=False):
    event_id = event['id'].lstrip('_')
    if not dryrun:
        try:
            response = service_client.events().delete(calendarId=calendar, eventId=event_id, sendUpdates='all').execute()
            logging.info('         Event deleted: %s' % str(event))
        except Exception as e:
            logging.error('Exception deleting event (%s) from calendar: %s' % (str(event), str(e)))

    else:
        logging.info('         Dryrun delete event from calendar(%s): %s' % (calendar, event['summary']))


def update_event_in_calendar(service_client, event, calendar, dryrun=False):
    event_id = event['id'].lstrip('_')
    updated_event_body = {}
    updated_event_body['start'] = event['start']
    updated_event_body['end'] = event['end']
    if 'summary' in event:
        updated_event_body['summary'] = event['summary']
    if 'description' in event:
        updated_event_body['description'] = event['description']
        if not 'Synced by google-calendar-syncer' in updated_event_body['description']:
            updated_event_body['description'] = updated_event_body['description'] + '\n\nSynced by google-calendar-syncer'
    else:
        updated_event_body['description'] = 'Synced by google-calendar-syncer'
    if 'location' in event:
        updated_event_body['location'] = event['location']
    if 'recurrence' in event:
        updated_event_body['recurrence'] = event['recurrence']
    if 'reminders' in event:
        updated_event_body['reminders'] = event['reminders']

    if not dryrun:
        response = service_client.events().update(calendarId=calendar, eventId=event_id, body=updated_event_body, sendUpdates='all').execute()
        logging.info('Event updated: %s' % response['summary'])
        logging.info("      Date(s): %s - %s" % (parse_to_string(response['start']), parse_to_string(response['end'])))
    else:
        logging.info('Dryrun update event in calender(%s): %s' % (calendar, updated_event_body['summary']))


def sync_events_to_calendar(service_client, now, from_cal, from_cal_cache, from_cal_events, to_cal, limit=0, dryrun=False):
    events_to_delete = []
    events_to_insert = []
    events_to_update = []
    if from_cal_cache:
        logging.debug('      Found a cache for the source calendar with ID: %s' % from_cal)
        logging.debug('      Cached Calendar events:\n%s' % json.dumps(from_cal_cache, indent=4))
        # First find events to delete - these will exist in cache, but not in from_cal_events
        logging.debug('      Comparing cached events against Source calendar events')
        for cache_event in from_cal_cache:
            found_in_from = False
            cache_event_id = cache_event['id'].lstrip('_')
            for from_event in from_cal_events:
                if from_event['id'].lstrip('_') == cache_event_id:
                    found_in_from = True
                    break
            if not found_in_from:
                # This cache_event may need to be deleted
                now_time = dateutil.parser.parse(now)
                if 'dateTime' in cache_event['start']:
                    start_time = dateutil.parser.parse(cache_event['start']['dateTime'])
                    time_diff = start_time - now_time
                    if not (time_diff.days < 0):
                        logging.debug('         Cache event with ID: %s should be deleted' % cache_event['id'])
                        events_to_delete.append(cache_event)
                elif 'date' in cache_event['start']:
                    # all day event
                    start_day = cache_event['start']['date']
                    today = datetime.date.today().isoformat()
                    if start_day > today:
                        logging.debug('         Cache event with ID: %s should be deleted' % cache_event['id'])
                        events_to_delete.append(cache_event)

        # Now find:
        #    events to insert - these will exist in from_cal_events, but not in cache
        #    events to update - these will exist in both cache and from_cal_events, with different updated times
        for from_event in from_cal_events:
            found_in_cache = False
            for cache_event in from_cal_cache:
                if cache_event['id'].lstrip('_') == from_event['id'].lstrip('_'):
                    # Found it - check the updated time
                    found_in_cache = True
                    from_event_updated_time = dateutil.parser.parse(from_event['updated'])
                    cache_event_updated_time = dateutil.parser.parse(cache_event['updated'])
                    time_diff = cache_event_updated_time - from_event_updated_time
                    # if the from_event has a later updated time, we need to update the event
                    if time_diff.days < 0:
                        # Add this to the events_to_update list
                        logging.debug('         Cache event with ID: %s should be updated' % cache_event['id'])
                        events_to_update.append(cache_event)
            if not found_in_cache:
                # Didn't find the event ID in the cached events - it must be new
                logging.debug('         Calendar event with ID: %s should be inserted' % from_event['id'])
                events_to_insert.append(from_event)

        if len(events_to_delete) == 0 and len(events_to_insert) == 0 and len(events_to_update) == 0:
            logging.info('      No changes found!')
        else:
            if len(events_to_delete) > 0:
                logging.info('      Found some events to delete')
                # Delete any old events
                for old_event in events_to_delete:
                    delete_from_calendar(service_client, old_event, to_cal, dryrun)

            if len(events_to_insert) > 0:
                logging.info('      Found some new events to add')
                # Insert any new events
                for new_event in events_to_insert:
                    insert_into_calendar(service_client, new_event, to_cal, dryrun)

            if len(events_to_update) > 0:
                logging.info('      Found some events that need updating')
                # Update events that need updating
                for update_event in events_to_update:
                    update_event_in_calendar(service_client, update_event, to_cal, dryrun)
    else:
        # No cache present - need to get events from the destination calendar and compare
        logging.debug('      No cache present - need to get events from destination calendar for comparison')
        logging.debug('      Getting all events for destination calendar with ID: %s' % to_cal)
        to_calendar_events = get_events_for_calendar(now, service_client, to_cal, limit)
        logging.debug('      Destination Calendar events:\n%s' % json.dumps(to_calendar_events, indent=4))

        insert_count=0
        update_count=0

        # Need to loop through the from_cal_events and see if we can find a matching one in the to_calendar_events
        # If we can't find it, then we need to insert the event into the to_calendar
        for from_event in from_cal_events:
            found = False
            for to_event in to_calendar_events:
                # check to see if this to_event matches the from_event
                if to_event['id'] == from_event['id'].lstrip('_'):
                    found = True
                    # We found a match - check the updated time
                    from_event_updated_time = dateutil.parser.parse(from_event['updated'])
                    to_event_updated_time = dateutil.parser.parse(to_event['updated'])
                    time_diff = to_event_updated_time - from_event_updated_time
                    # if the from_event has a later updated time, we need to update the event
                    if time_diff.days < 0:
                        # Need to update this event
                        update_event_in_calendar(service_client, from_event, to_cal, dryrun)
                        update_count += 1
                        break
            if not found:
                insert_into_calendar(service_client, from_event, to_cal, dryrun)
                insert_count += 1

        if insert_count == 0 and update_count == 0:
            logging.info('No changes found!')
        else:
            if insert_count > 0:
                logging.info('      Inserted %s new events into calendar: %s' % (str(insert_count), to_cal))
            if update_count > 0:
                logging.info('      Updated %s events in calendar: %s' % (str(insert_count), to_cal))


def lambda_handler(event, context):
    log_level = logging.INFO
    if 'DEBUG' in os.environ and os.environ['DEBUG'].lower() == "true":
        log_level = logging.DEBUG

    logger = logging.getLogger()
    logger.setLevel(log_level)

    logging.debug("Received event: " + json.dumps(event, indent=2))

    if not 'S3_BUCKET' in os.environ:
        logging.info('Missing S3_BUCKET environment variable - cannot continue')

    # config will live in an S3 bucket in the config.json file
    bucket = os.environ.get('S3_BUCKET')
    key = 'config.json'

    dryrun = False
    if 'DRYRUN' in os.environ:
        dryrun = True

    s3_client = boto3.client('s3')

    config = _get_config_from_s3(s3_client, bucket, key)

    storage_path = tempfile.mkdtemp()
    # Get the OAUTH credentials and cache (if it exists)
    _load_creds_and_cache(s3_client, bucket, storage_path)

    service = authorize(storage_path)

    # Time NOW - only look at events from this point forward...
    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time

    for item in config:
        logging.info('Processing %s' % item)
        dest_cal_id = config[item]['destination_cal_id']
        for src_cal in config[item]['source_cals']:
            logging.info('   Syncing events from %s' % src_cal['name'])
            src_cal_events = get_events_for_calendar(now, service, src_cal['cal_id'], 0)
            logging.debug('      Source Calendar events:\n%s' % json.dumps(src_cal_events, indent=4))
            # Get the src_cal cache
            src_cal_cache = _get_calendar_cache(storage_path, src_cal['cal_id'])
            sync_events_to_calendar(service, now, src_cal['cal_id'], src_cal_cache, src_cal_events, dest_cal_id, 0, dryrun)
            if not dryrun:
                # Update the calendar cache file
                _update_calendar_cache(storage_path, src_cal['cal_id'], src_cal_events)
                _update_s3_calendar_cache(s3_client, bucket, storage_path, src_cal['cal_id'])

    # Update the token file in S3
    token_key = 'token.json'
    token_path = storage_path + os.sep + 'token.json'
    _put_to_s3(s3_client, bucket, token_key, token_path)

    logging.info ('Cleaning up...')
    shutil.rmtree(storage_path)
    logging.info("DONE")
    return True


if __name__ == "__main__":
    LOG_FILENAME = 'google-calendar-syncer.log'

    parser = argparse.ArgumentParser(description='google-calendar-syncer')
    parser.add_argument("--init", help="Initialize - create token.json and credentials.json", action='store_true')
    parser.add_argument("--config", help="path to config file", dest='config')
    parser.add_argument("--src-cal-id", help="source cal ID", dest='src_cal_id')
    parser.add_argument("--dst-cal-id", help="destination cal ID", dest='dst_cal_id')
    parser.add_argument("--limit", help="Limit to next X events (0)", dest='limit', default=0)
    parser.add_argument("--profile", help="AWS Profile to use when communicating with S3", dest='profile')
    parser.add_argument("--region", help="AWS region S3 bucket is in", dest='region')
    parser.add_argument("--cleanup", help="Clean up temp folder after exection", action='store_true')
    parser.add_argument("--verbose", help="Turn on DEBUG logging", action='store_true', required=False)
    parser.add_argument("--dryrun", help="Do a dryrun - no changes will be performed", dest='dryrun',
                        action='store_true', default=False,
                        required=False)
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

    # Used for token.json, credentials.json and cache folder and files
    storage_path = '.'

    if args.config:
        if args.config.startswith('s3://'):
            s3_client = None
            if args.profile or args.region:
                session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
                s3_client = session.client('s3')
            else:
                s3_client = boto3.client('s3')

            s3_path = args.config.split('s3://')[1]
            s3_bucket = s3_path.split('/')[0]
            s3_key = s3_path.split('/', 1)[1]
            config = _get_config_from_s3(s3_client, s3_bucket, s3_key)
            storage_path = tempfile.mkdtemp()
            # Get the credentials and cache
            _load_creds_and_cache(s3_client, s3_bucket, storage_path)
        else:
            # Local config file
            if os.path.exists(args.config):
                with open(args.config, 'r') as f:
                    config = json.loads(f.read())
            else:
                logging.error("Config file doesn't exist at given path: %s" % args.config)
                exit(1)
    else:
        # Single source and destination calendar provided
        config = {
            "Destination Calendar" : {
                "destination_cal_id" : args.dst_cal_id,
                "source_cals" : [
                    {
                        "name" : "Source Calendar",
                        "cal_id" : args.src_cal_id
                    }
                ]
            }
        }

    logging.debug("STARTING RUN")

    service = authorize(storage_path)

    # Time NOW - only look at events from this point forward...
    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time

    for item in config:
        logging.info('Processing %s' % item)
        dest_cal_id = config[item]['destination_cal_id']
        for src_cal in config[item]['source_cals']:
            logging.info('   Syncing events from %s' % src_cal['name'])
            src_cal_events = get_events_for_calendar(now, service, src_cal['cal_id'], 0)
            # Get the src_cal cache
            src_cal_cache = _get_calendar_cache(storage_path, src_cal['cal_id'])
            sync_events_to_calendar(service, now, src_cal['cal_id'], src_cal_cache, src_cal_events, dest_cal_id, args.limit, args.dryrun)

            if not args.dryrun:
                # Update the calendar cache file
                logging.info('Updating calendar cache for calendar with ID: %s' % src_cal['cal_id'])
                if args.config.startswith('s3://'):
                    _update_calendar_cache(storage_path, src_cal['cal_id'], src_cal_events)
                    _update_s3_calendar_cache(s3_client, s3_bucket, storage_path, src_cal['cal_id'])
                else:
                    _update_calendar_cache(storage_path, src_cal['cal_id'], src_cal_events)

    if args.cleanup:
        logging.info ('Cleaning up...')
        shutil.rmtree(storage_path)

    logging.info("DONE")
