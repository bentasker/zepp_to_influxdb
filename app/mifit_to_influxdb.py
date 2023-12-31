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
            elif sleep['mode'] == 7:
                stage = 'awake'                
            elif sleep['mode'] == 8:
                stage = 'REM'                
            else:
                stage = f"unknown_{sleep['mode']}"
                
            start_epoch = minute_to_timestamp(sleep['start'], day)
            
            row = {
                "timestamp": start_epoch * 1000000000, # Convert to nanos 
                "fields" : {
                    "total_sleep_min" : sleep['stop'] - sleep['start']
                    },
                "tags" : {
                    "activity_type" : "sleep_stage",
                    "sleep_type" : stage
                    }
            }           
            rows.append(row)

            # Create points for every minute in this state.
            stop_epoch = minute_to_timestamp(sleep['stop'], day)
            
            s = start_epoch
            while s <= stop_epoch:
                row = {
                    "timestamp": s * 1000000000, # Convert to nanos 
                    "fields" : {
                        "current_sleep_state" : stage,
                        "current_sleep_state_int" : sleep['mode'],
                        },
                    "tags" : {
                        "activity_type" : "sleep_stage_tracker"
                        }
                }           
                rows.append(row)
                s += 60
            
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
        row['fields'][f"recorded_sleep_{stage}_events"] = stages_counters[stage]
        
    # Add the record
    rows.append(row)    
    
    return rows
    

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
            
            start_epoch = minute_to_timestamp(activity['start'], day)
            
            row = {
                "timestamp": start_epoch * 1000000000, # Convert to nanos TODO 
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
            
            # Create an entry for each minute of this activity
            end_epoch = minute_to_timestamp(activity['stop'], day)
            s = start_epoch
            while s <= end_epoch:
                row = {
                    "timestamp": s * 1000000000, # Convert to nanos 
                    "fields" : {
                        "current_activity_type" : activity_type,
                        "current_activity_type_int" : activity['mode'],
                        },
                    "tags" : {
                        "activity_type" : "activity_type_tracker"
                        }
                }           
                rows.append(row)
                s += 60

            
            
    # Record the number of activities
    row = {
        "timestamp": int(ts) * 1000000000, # Convert to nanos 
        "fields" : {
            "recorded_activities" : activity_count
            },
        "tags" : {}
    }
    for activity in activity_counters:
        row['fields'][f"recorded_activity_{activity}_events"] = activity_counters[activity]
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
        'query_type': 'detail',
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
        
        if "data_hr" in daydata:
            print("Extracting heart rate")
            result_set = result_set + translate_heartrate_blob(daydata)
            
        summary=json.loads(base64.b64decode(daydata['summary']))
        for k,v in summary.items():
            if k=='stp':
                # Extract step data
                result_set = result_set + extract_step_data(ts, v, day)
            elif k=='slp':
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


def translate_heartrate_blob(daydata):
    ''' Extract the heart rate data blob from the JSON
    and convert to a list of stats
    
    '''
    
    # Create a datetime object from the date specified in JSON
    # this will be midnight.
    nowtime = datetime.datetime.strptime(daydata['date_time'], "%Y-%m-%d")
        
    number_blob = bytearray(base64.b64decode(daydata['data_hr']))
    #print(number_blob)
    adjusted_vals = []
    
    # Initialise values
    x = 1
    b=b''    
   
    # Iterate through the bytestring
    for byte_i in number_blob:
        # iterating over leads to us fetching ints
        # not bytes, so convert back
        byte = byte_i.to_bytes(length=1, byteorder="big")
        
        # Concatenate this byte onto the previous
        b += byte
        
        # Move the marker to the right
        x += 1
        
        # The data is a java short, so every
        # 2 bytes, convert it to an integer
        if x == 2:            
            # Convert the bytestring to an int
            v = int(b.hex(), 16)
            
            # Adjust the timestamp forward 1 minute
            nowtime = nowtime + datetime.timedelta(minutes=1)
            
            # They seem to use a high initialisation
            # value to indicate lack of data. If it's
            # higher than 200 skip it
            if v < 200:
                # Append a point
                adjusted_vals.append({
                        "timestamp": int(nowtime.strftime('%s')) * 1000000000, # Convert to nanos
                        "fields" : {
                            "heart_rate" : int(v),
                            },
                        "tags" : {
                            "hr_measure" : "periodic"
                            }
                    })
                
            
            # Reset the byte string
            b = b''
            # Reset the counter
            x = 1

    return adjusted_vals
    
    
def get_blood_oxygen_data(auth_info, config):    
    ''' Retrieve stress level information
    '''
    rows = []
    
    ''' calculate the times that the api query should check between
    '''
    today = datetime.datetime.today()
    today_end = datetime.datetime.combine(today, datetime.datetime.max.time()) 
    
    query_start_d = today - datetime.timedelta(days=config['QUERY_DURATION'])
    # Make it midnight  - the api doesn't seem to like mid-day queries
    query_start = datetime.datetime.combine(query_start_d, datetime.datetime.min.time())
    
    
    print("Retrieving blood oxygen data")
    band_data_url=f"https://api-mifit.zepp.com/users/{auth_info['token_info']['user_id']}/events"
    headers={
        'apptoken': auth_info['token_info']['app_token'],
    }
    data={
        'from': query_start.strftime('%s000'),
        'to': today_end.strftime('%s000'),
        "eventType": "blood_oxygen",
        "limit": 1000,
        "timeZone" : "Europe/London"
    }
    response=requests.get(band_data_url,params=data,headers=headers)
    r_json = response.json()

    if "items" not in r_json:
        return rows
    
    for blood in r_json['items']:
        if blood['subType'] == "odi":
            rows.append(processODIEvent(blood))
        elif blood['subType'] == "osa_event":
            rows.append(processOSAEvent(blood))
        elif blood['subType'] == "click":
            rows.append(processBloodClickEvent(blood))


    return rows

def processBloodClickEvent(record):
    ''' Process a "click" event
    
    This appears to be the user manually triggering a blood
    oxygen reading from the watch/band (utilities/zepp_to_influxdb#6)
    '''
    extra = json.loads(record['extra'])
    return {
        "timestamp": int(record['timestamp']) * 1000000, # Convert to nanos 
        "fields" : {
            "spo2_level" : float(extra['spo2']),
            },
        "tags" : {
            "blood_event" : "manual_read"
            }
    }   
        
    
def processOSAEvent(record):
    ''' Process a possible Obstructive Sleep Apnea event
    '''
    
    osa_record = json.loads(record['extra'])
    
    return {
        "timestamp": int(record['timestamp']) * 1000000, # Convert to nanos 
        "fields" : {
            "spo2_decrease" : float(osa_record['spo2_decrease']),
            },
        "tags" : {
            "blood_event" : "osa"
            }
    }           

    
def processODIEvent(record):
    ''' Process an ODI event
    '''
    return {
        "timestamp": int(record['timestamp']) * 1000000, # Convert to nanos 
        "fields" : {
            "odi_read" : float(record['odi']),
            # Not sure what this is, skipping for now 
            # (I *think* it might just be a record ID)
            # "odi_number" : int(record['odiNum']),
            #
            # There are also "cost" and "valid"
            "score" : float(record['score']),
            },
        "tags" : {
            "blood_event" : "odi"
            }
    }       

def get_PAI_data(auth_info, config):
    ''' Retrieve Personal Actitivity Intelligence scoring data
    
    TODO: This could definitely be DRYer
    '''
    rows = []
    
    ''' calculate the times that the api query should check between
    '''
    today = datetime.datetime.today()
    today_end = datetime.datetime.combine(today, datetime.datetime.max.time()) 
    
    query_start_d = today - datetime.timedelta(days=config['QUERY_DURATION'])
    # Make it midnight  - the api doesn't seem to like mid-day queries
    query_start = datetime.datetime.combine(query_start_d, datetime.datetime.min.time())
    
    
    print("Retrieving PAI data")
    band_data_url=f"https://api-mifit-de2.zepp.com/users/{auth_info['token_info']['user_id']}/events"
    headers={
        'apptoken': auth_info['token_info']['app_token'],
    }
    data={
        "limit" : 1000,
        "from" : query_start.strftime('%s000'),
        "to" : today_end.strftime('%s000'),
        "eventType" : "PaiHealthInfo",
        "timeZone" : "Europe/London"
    }   
    
    response=requests.get(band_data_url,params=data,headers=headers)
    r_json = response.json()
    if "items" not in r_json:
        return rows
    
    # Iterate through the daily entries
    for item in r_json['items']:
        timestamp_ms = int(item['timestamp'])

        # Heart rates       
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "heart_rate" : int(item['maxHr']),
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "hr_measure" : "PAI",
                    "hr_state" : "max"
                    }
            }        
        rows.append(row)
        
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "heart_rate" : int(item['restHr']),
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "hr_measure" : "PAI",
                    "hr_state" : "resting"
                    }
            }        
        rows.append(row)        
        
        
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "activity_duration_m" : int(item['lowZoneMinutes']),
                    "pai_score_bound" : int(item['lowZoneLowerLimit']),
                    "pai_score" : float(item['lowZonePai'])
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "PAI_bound" : "low"
                    }
            }
        rows.append(row)      
        
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "activity_duration_m" : int(item['mediumZoneMinutes']),
                    "pai_score_bound" : int(item['mediumZoneLowerLimit']),
                    "pai_score" : float(item['mediumZonePai'])
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "PAI_bound" : "medium"
                    }
            }
        rows.append(row)        
        
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "activity_duration_m" : int(item['highZoneMinutes']),
                    "pai_score_bound" : int(item['highZoneLowerLimit']),
                    "pai_score" : float(item['highZonePai'])
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "PAI_bound" : "high"
                    }
            }
        rows.append(row)          
                
        row = {
                "timestamp": timestamp_ms * 1000000, # Convert to nanos
                "fields" : {
                    "scorable_activities" : len(item['activityScores']),
                    "pai_score" : float(item['dailyPai']),
                    "total_pai" : float(item['totalPai'])
                    },
                "tags" : {
                    "PAI_measure" : "daily",
                    "PAI_bound" : "daily"
                    }
            }
        rows.append(row)            
    
    return rows



def get_stress_data(auth_info, config):    
    ''' Retrieve stress level information
    '''
    rows = []
    
    ''' calculate the times that the api query should check between
    '''
    today = datetime.datetime.today()
    today_end = datetime.datetime.combine(today, datetime.datetime.max.time()) 
    
    query_start_d = today - datetime.timedelta(days=config['QUERY_DURATION'])
    # Make it midnight  - the api doesn't seem to like mid-day queries
    query_start = datetime.datetime.combine(query_start_d, datetime.datetime.min.time())
    
    
    print("Retrieving stress data")
    band_data_url=f"https://api-mifit.zepp.com/users/{auth_info['token_info']['user_id']}/events"
    headers={
        'apptoken': auth_info['token_info']['app_token'],
    }
    data={
        'from': query_start.strftime('%s000'),
        'to': today_end.strftime('%s000'),
        "eventType": "all_day_stress",
        "limit": 1000
    }
    response=requests.get(band_data_url,params=data,headers=headers)
    r_json = response.json()
    if "items" not in r_json:
        return rows
    
    for stress in r_json['items']:
        row = {
            "timestamp": int(stress['timestamp']) * 1000000, # Convert to nanos 
            "fields" : {
                "minimum_stress_level" : int(stress['minStress']),
                "max_stress_level" : int(stress['maxStress']),
                "mean_stress_level" : int(stress['avgStress']),
                "relaxed_time_perc" : int(stress['relaxProportion']),
                "normal_stress_time_perc" : int(stress['normalProportion']),
                "medium_stress_time_perc" : int(stress['mediumProportion']),
                "high_stress_time_perc" : int(stress['highProportion'])
                },
            "tags" : {
                "stress" : "daily"
                }
        }           
        rows.append(row)
        
        # See whether we've been provided regular reads
        if "data" in stress:
            stress_dump = json.loads(stress['data'])
            for stresspoint in stress_dump:
                row = {
                    "timestamp": int(stresspoint['time']) * 1000000, # Convert to nanos 
                    "fields" : {
                        "current_stress_level" : int(stresspoint['value'])
                        },
                    "tags" : {
                        "stress" : "point_in_time"
                        }
                }           
                rows.append(row)                

    return rows

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
    
    try:
        stress_rows = get_stress_data(auth_info, config)
        result_set = result_set + stress_rows
    except:
        print("Failed to collect stress data")
    
    
    try:
        blood_o2 = get_blood_oxygen_data(auth_info, config)
        result_set = result_set + blood_o2
    except:
        print("Failed to collect blood oxygen data")
    
    
    try:
        pai = get_PAI_data(auth_info, config)
        result_set = result_set + pai
    except:
        print("Failed to collect PAI information")
        
    
    # Write into InfluxDB
    write_results(result_set, serial, config)


if __name__== "__main__":
    main()
