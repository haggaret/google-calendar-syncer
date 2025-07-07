#Google Calendar Syncer

Small utility to keep events from source calendar(s) updated in a destination calendar. This utility uses the Google API
to query the source calendar(s) for all events in the future and sync them to the destination calendar. It will create a
cache file for each source calendar that will be used on subsequent runs to determine what needs to be
deleted/updated/inserted. This utility can be run from the command line as follows:

```
usage: google-calendar-syncer.py [-h] [--init] [--config CONFIG]
                                 [--src-cal-id SRC_CAL_ID]
                                 [--dst-cal-id DST_CAL_ID] [--limit LIMIT]
                                 [--profile PROFILE] [--region REGION]
                                 [--cleanup] [--verbose] [--dryrun]
google-calendar-syncer

optional arguments:
  -h, --help            show this help message and exit
  --init                Initialize - create token.json and credentials.json
  --config CONFIG       path to config file
  --src-cal-id SRC_CAL_ID
                        source cal ID
  --dst-cal-id DST_CAL_ID
                        destination cal ID
  --limit LIMIT         Limit to next X events (0)
  --profile PROFILE     AWS Profile to use when communicating with S3
  --region REGION       AWS region S3 bucket is in
  --cleanup             Clean up temp folder after exection
  --verbose             Turn on DEBUG logging
  --dryrun              Do a dryrun - no changes will be performed```
```

Config file is a json document with the following info:

```json
{
  "Destination Calendar 1" : {
    "destination_cal_id": "<destination_calendar_id>",
    "source_cals": {
      "Source Calendar Name": "<source_calendar_id>"
    },
    "exclusions": {
      "summary": [
        "Summary Exclusion Text"
      ]
    },
    "filters": {
      "title": {
        "contains": [
          "filter contains text"
        ]
      }
    }
  },
  "Destination Calendar 2" : {
    "destination_cal_id" : "<destination_calendar_id>",
    "source_cals" :
      {
        "Source Calendar 1": "<source_calendar_id>",
        "Source Calendar 2": "<source_calendar_id>"
      }
  }
}
```

###examples:

using a config file:
```bash
python google-calendar-syncer.py --config /path/to/config.json
```

supplying source and destination calendar IDs directly:
```bash
python google-calendar-syncer.py --src-cal-id <source_calendar_id> --dst-cal-id <destination_calendar_id>
```

####Note:
- the cache folder and files (and credential files) are created relative to the working directory where the script is run from

## Deploying as a scheduled lambda
If desired, this utility can also be deployed as a scheduled lambda, that will run at a given frequency.

### Prerequisites

The cloudformation template that will be used to deploy the scheduled lambda does not currently create the S3 bucket
where the credential files and cache will live. That bucket should be created prior to deploying the lambda.

In addition to creating the S3 bucket, the required credential files (token.json and credentials.json) should also be
generated. This can be accomplished by running the script once manually, using the --init option. The files should be
created in the same directory that the script is run from. Once created, upload these files to the config S3 bucket.

eg.
```bash
python google-calendar-syncer.py --init
```

Build and Deploy is accomplished using [AWS SAM](https://github.com/awslabs/serverless-application-model).

1) ```sam build --use-container```
2) ```sam deploy```

Note: You can create a `samconfig.toml` file to eliminate the need to input anything for the deploy, eg.

```
version = 0.1
[default]
[default.deploy]
[default.deploy.parameters]
stack_name = "google-calendar-syncer"
s3_bucket = "<lambda-deploy-bucket>"
s3_prefix = "google-calendar-syncer"
region = "<aws-region>"
confirm_changeset = false
capabilities = "CAPABILITY_NAMED_IAM"
parameter_overrides = [
  "Debug=false",
  "TableName=TableName",
  "Schedule='rate(10 minutes)'"
]
```

Running the deploy will create or update a cloudformation stack.

####Note:
- the bucket_name referenced above will likely be a *different* bucket than was used during the package step above
- see [here](https://docs.aws.amazon.com/AmazonCloudWatch/latest/events/ScheduledEvents.html) for schedule_expression values


## Running using Docker (under development)

If desired, the script can be run within a Docker container (if a python environment isn't readily available)

First build the image using the included Dockerfile

```bash
docker build . -t google-calendar-syncer
```

Note that you will need to mount a few files into the container, namely:
- config.json file
- cache folder
- credentials.json file (for google authentication)
- token.json file (for google authentication)


Run the container:

```bash
docker run -v /path/to/config.josn:/app/config.json \
 -v /path/to/cache:/app/cache \
 -v /path/to/credentials.json:/app/credentials.json \
 -v /path/to/token.json:/app/token.json
 google-calendar-syncer --config /app/config.json
```

At this point, the only way I've been able to generate the credentials.json and token.json files is by running the
script outside of the docker environment once and then mounting the files created as mentioned above.