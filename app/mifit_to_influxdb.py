#!/usr/bin/env python3
#
#
# Mi-Fit API to InfluxDB
#
# Polls the Mi-Fit/Zepp API to retrieve smart-band information
# then writes stepcounts etc onwards to InfluxDB
#
# Credit for API comms approach goes to Michael Wyraz
# https://github.com/micw/hacking-mifit-api
#
# pip install influxdb-client
'''
Copyright (c) 2023 B Tasker, M Wyraz

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

    Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

    Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

    Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import base64
import datetime
import json
import os
import requests
import sys
import urllib.parse

from influxdb_client import InfluxDBClient, Point


def fail(message):
    print("Error: {}".format(message))
    quit(1)

def mifit_auth_email(email,password):
    ''' Log into the Mifit API using username and password
        in order to acquire an access token
    '''
    print("Logging in with email {}".format(email))
    auth_url='https://api-user.huami.com/registrations/{}/tokens'.format(urllib.parse.quote(email))
    data={
        'state': 'REDIRECTION',
        'client_id': 'HuaMi',
        'redirect_uri': 'https://s3-us-west-2.amazonws.com/hm-registration/successsignin.html',
        'token': 'access',
        'password': password,
    }
    response=requests.post(auth_url,data=data,allow_redirects=False)
    response.raise_for_status()
    redirect_url=urllib.parse.urlparse(response.headers.get('location'))
    response_args=urllib.parse.parse_qs(redirect_url.query)
    if ('access' not in response_args):
        fail('No access token in response')
    if ('country_code' not in response_args):
        fail('No country_code in response')

    print("Obtained access token")
    access_token=response_args['access'];
    country_code=response_args['country_code'];
    return mifit_login_with_token({
        'grant_type': 'access_token',
        'country_code': country_code,
        'code': access_token,
    })

def mifit_login_with_token(login_data):
    ''' Log into the API using an access token
    
        This is the second stage of the login process
    '''
    login_url='https://account.huami.com/v2/client/login'
    data={
        'app_name': 'com.xiaomi.hm.health',
        'dn': 'account.huami.com,api-user.huami.com,api-watch.huami.com,api-analytics.huami.com,app-analytics.huami.com,api-mifit.huami.com',
        'device_id': '02:00:00:00:00:00',
        'device_model': 'android_phone',
        'app_version': '4.0.9',
        'allow_registration': 'false',
        'third_name': 'huami',
    }
    data.update(login_data)
    response=requests.post(login_url,data=data,allow_redirects=False)
    result=response.json()
    return result;

def minutes_as_time(minutes):
    ''' Convert a minute counter to a human readable time 
    '''
    return "{:02d}:{:02d}".format((minutes//60)%24,minutes%60)

def dump_sleep_data(day, slp):
    ''' Output the collected sleep data 
    '''
    print("Total sleep: ",minutes_as_time(slp['lt']+slp['dp']),
        ", deep sleep",minutes_as_time(slp['dp']),
        ", light sleep",minutes_as_time(slp['lt']),
        ", slept from",datetime.datetime.fromtimestamp(slp['st']),
        "until",datetime.datetime.fromtimestamp(slp['ed']))
    if 'stage' in slp:
        for sleep in slp['stage']:
            if sleep['mode']==4:
                sleep_type='light sleep'
            elif sleep['mode']==5:
                sleep_type='deep sleep'
            else:
                sleep_type="unknown sleep type: {}".format(sleep['mode'])
            print(format(minutes_as_time(sleep['start'])),"-",minutes_as_time(),
                sleep_type)

def extract_sleep_data(ts, slp, day):
    ''' Extract sleep data and format it for feeding into InfluxDB
    '''
    rows = []
    row = {
        "timestamp": int(ts) * 1000000000, # Convert to nanos 
        "fields" : {
            "total_sleep_min" : slp['lt']+slp['dp'],
            "deep_sleep_min" : slp['dp'],
            "rem_sleep_min" : slp['lt'],
            "slept_from" : str(datetime.datetime.fromtimestamp(slp['st'])),
            "slept_to" : str(datetime.datetime.fromtimestamp(slp['ed'])),
            },
        "tags" : {
            "activity_type" : "sleep"
            }
    }
        
    rows.append(row)
    
    sleep_stages = 0
    stages_counters = {}
    # If there are stages recorded, also log those
    if 'stage' in slp:
        sleep_stages = len(slp['stage'])
        for sleep in slp['stage']:
            if sleep['mode'] == 4:
                stage = 'light_sleep'
            elif sleep['mode'] == 5:
                stage = 'deep_sleep'
            else:
                stage = f"unknown_{sleep['mode']}"
                
            row = {
                "timestamp": minute_to_timestamp(sleep['start'], day) * 1000000000, # Convert to nanos 
                fields : {
                    "total_sleep_min" : sleep['stop'] - sleep['start']
                    },
                tags : {
                    "activity_type" : "sleep_stage",
                    "sleep_type" : stage
                    }
            }           
            rows.append(row)
    
            # Increment the counter for the type
            # initialising if not already present
            if stage not in stages_counters:
                stages_counters[stage] = 0
            stages_counters[stage] += 1
    
    
    # Record the number of sleep stages
    row = {
        "timestamp": int(ts) * 1000000000, # Convert to nanos 
        "fields" : {
            "recorded_sleep_stages" : sleep_stages
            },
        "tags" : {}
    }
        
    # Add a field for each of the recorded stages_counters 
    for stage in stages_counters:
        row['fields'][f"recorded_{stage}_events"] = stages_counters[stage]
        
    # Add the record
    rows.append(row)    
    
    return rows
    
    
    
    
def dump_step_data(day, stp):
    ''' Output the collected step data 
    '''
    print("Total steps: ",stp['ttl'],", used",stp['cal'],"kcals",", walked",stp['dis'],"meters")
    if 'stage' in stp:
        for activity in stp['stage']:
            if activity['mode']==1:
                activity_type='slow walking'
            elif activity['mode']==3:
                activity_type='fast walking'
            elif activity['mode']==4:
                activity_type='running'
            elif activity['mode']==7:
                activity_type='light activity'
            else:
                activity_type="unknown activity type: {}".format(activity['mode'])
            print(format(minutes_as_time(activity['start'])),"-",minutes_as_time(activity['stop']),
                activity['step'],'steps',activity_type)

def extract_step_data(ts, stp, day):
    ''' Extract step data and return in a format ready for feeding
        into InfluxDB
    '''
    rows = []
    row = {
        "timestamp": int(ts) * 1000000000, # Convert to nanos 
        "fields" : {
            "total_steps" : stp['ttl'],
            "calories" : stp['cal'],            
            "distance_m" : stp['dis']
            },
        "tags" : {
            "activity_type" : "steps"
            }
    }
        
    rows.append(row)
    
    activity_count = 0
    activity_counters = {}
    # Iterate through any listed stages
    if "stage" in stp:
        activity_count = len(stp['stage'])        
        for activity in stp['stage']:
            if activity['mode'] == 1:
                activity_type = 'slow_walking'
            elif activity['mode'] == 3:
                activity_type = 'fast_walking'
            elif activity['mode'] == 4:
                activity_type = 'running'
            elif activity['mode'] == 7:
                activity_type = 'light_activity'
            else:
                activity_type = f"unknown_{activity['mode']}"
                
            row = {
                "timestamp": minute_to_timestamp(activity['start'], day) * 1000000000, # Convert to nanos TODO 
                "fields" : {
                    "total_steps" : activity['step'],
                    "calories" : activity['cal'],
                    "distance_m" : stp['dis'],
                    "activity_duration_m" : activity['stop'] - activity['start'],            
                    },
                "tags" : {
                    "activity_type" : activity_type
                    }
            }
            rows.append(row)
            
            # Increment the type specific counter
            if activity_type not in activity_counters:
                activity_counters[activity_type] = 0
            activity_counters[activity_type] += 1
            
            
    # Record the number of activities
    row = {
        "timestamp": int(ts) * 1000000000, # Convert to nanos 
        "fields" : {
            "recorded_activities" : activity_count
            },
        "tags" : {}
    }
    for activity in activity_counters:
        row['fields'][f"recorded_{activity}_events"] = activity_counters[activity]
    rows.append(row)
        
            
    return rows
    
def minute_to_timestamp(minute, day):
    ''' Take a count of minutes into the day and a date, then turn into an
    epoch timestamp
    '''
    
    time_norm = minutes_as_time(minute)
    date_string = f"{day} {time_norm}"
    epoch = int(datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M").strftime('%s'))
    return epoch
    
    
def get_band_data(auth_info, config):
    ''' Retrieve information for the band/watch associated with the account
    '''
    result_set = []
    serial = "unknown"
    
    ''' We need to calculate today's midnight so that we can assess whether
    a date entry relates to today or not.
    
    The idea being that we want to write points throughout the current day
    but for previous days only want to write/update the previous entry
    
    The value of today will also be used to construct the query string presented
    to the api
    '''
    today = datetime.datetime.today()
    midnight = datetime.datetime.combine(today, datetime.datetime.min.time())
    today_ts = today.strftime('%s')    
    
    query_start = today - datetime.timedelta(days=config['QUERY_DURATION'])
    
    
    print("Retrieving mi band data")
    band_data_url='https://api-mifit.huami.com/v1/data/band_data.json'
    headers={
        'apptoken': auth_info['token_info']['app_token'],
    }
    data={
        'query_type': 'summary',
        'device_type': 'android_phone',
        'userid': auth_info['token_info']['user_id'],
        'from_date': query_start.strftime('%Y-%m-%d'),
        'to_date': today.strftime('%Y-%m-%d'),
    }
    response=requests.get(band_data_url,params=data,headers=headers)
    
    for daydata in response.json()['data']:
        day = daydata['date_time']
        print(day)
        # Parse the date into a datetime
        day_ts = datetime.datetime.strptime(daydata['date_time'], '%Y-%m-%d')
        
        # If day_ts is < midnight we want to set the timestamp to be 23:59:59 
        # on that day. If not, then we use the current timestamp.
        if day_ts < midnight:
            ts = datetime.datetime.combine(day_ts, datetime.datetime.max.time()).strftime('%s')
        else:
            ts = today_ts
        
        summary=json.loads(base64.b64decode(daydata['summary']))
        for k,v in summary.items():
            if k=='stp':
                # dump_step_data(day,v)
                # Extract step data
                result_set = result_set + extract_step_data(ts, v, day)
            elif k=='slp':
                # dump_sleep_data(day,v)
                # Extract the data
                result_set = result_set + extract_sleep_data(ts, v, day)
            elif k == "goal":
                result_set.append({
                    "timestamp": int(ts) * 1000000000, # Convert to nanos
                    "fields" : {
                        "step_goal" : int(v),
                        },
                    "tags" : {}
                })
            elif k == "sn":
                serial = v
            elif k == "sync":
                result_set.append({
                    "timestamp": int(ts) * 1000000000, # Convert to nanos
                    "fields" : {
                        "last_sync" : int(v),
                        },
                    "tags" : {}
                })                
            else:
                print(f"Skipped {k} = {v}")
                
    return result_set, serial

def write_results(results, serial, config):
    ''' Open a connection to InfluxDB and write the results in
    '''
    with InfluxDBClient(url=config['INFLUXDB_URL'], token=config['INFLUXDB_TOKEN'], org=config['INFLUXDB_ORG']) as _client:
        with _client.write_api() as _write_client:
            # Iterate through the results generating and writing points
            for row in results:
                p = Point(config['INFLUXDB_MEASUREMENT'])
                for tag in row['tags']:
                    p = p.tag(tag, row['tags'][tag])
                
                p = p.tag("serial_num", serial)
                
                for field in row['fields']:
                    p = p.field(field, row['fields'][field])
                    
                p = p.time(row['timestamp'])
                _write_client.write(config['INFLUXDB_BUCKET'], config['INFLUXDB_ORG'], p)


def main():
    ''' Main entry point
    '''
    
    # Collect config
    config = {}
    
    # InfluxDB settings
    config['INFLUXDB_URL'] = os.getenv("INFLUXDB_URL", False)
    config['INFLUXDB_TOKEN'] = os.getenv("INFLUXDB_TOKEN", "")
    config['INFLUXDB_ORG'] = os.getenv("INFLUXDB_ORG", "")
    config['INFLUXDB_MEASUREMENT'] = os.getenv("INFLUXDB_MEASUREMENT", "zepp")
    config['INFLUXDB_BUCKET'] = os.getenv("INFLUXDB_BUCKET", "telegraf")
    
    # How many days data should we request from the API?
    config['QUERY_DURATION'] = int(os.getenv("QUERY_DURATION", 2))
    
    # Get the Zepp credentials
    config['ZEPP_EMAIL'] = os.getenv("ZEPP_EMAIL", False)
    config['ZEPP_PASS'] = os.getenv("ZEPP_PASS", False)
    
    if not config['ZEPP_EMAIL'] or not config['ZEPP_PASS']:
        print("Error: Credentials not provided")
        sys.exit(1)
    
    # Get logged in
    auth_info=mifit_auth_email(config['ZEPP_EMAIL'], config['ZEPP_PASS'])
    
    # Fetch band info
    result_set, serial = get_band_data(auth_info, config)
    
    # Write into InfluxDB
    write_results(result_set, serial, config)


if __name__== "__main__":
    main()
