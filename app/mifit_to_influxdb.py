#!/usr/bin/env python3
#
#
# Mi-Fit API to InfluxDB
#
# Polls the Mi-Fit/Zepp API to retrieve smart-band information
# then writes stepcounts etc onwards to InfluxDB
#
# Credit for API comms approach goes to https://github.com/micw/hacking-mifit-api
#
# pip install influxdb-client


import argparse
import base64
import datetime
import json
import os
import requests
import urllib.parse

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


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

def extract_sleep_data(ts, slp):
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
	
	# If there are stages recorded, also log those
	if 'stage' in slp:
		for sleep in slp['stage']:
			if sleep['mode'] == 4:
				stage = 'light_sleep'
			elif sleep['mode'] == 5:
				stage = 'deep_sleep'
			else:
				stage = f"unknown_{sleep['mode']}"
				
			row = {
				"timestamp": sleep['start'] * 60 * 1000000000, # Convert to nanos 
				fields : {
					"total_sleep_min" : sleep['stop'] - sleep['start']
					},
				tags : {
					"activity_type" : "sleep_stage",
					"sleep_type" : stage
					}
			}			
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

def get_band_data(auth_info):
	''' Retrieve information for the band/watch associated with the account
	'''
	result_set = []
	print("Retrieveing mi band data")
	band_data_url='https://api-mifit.huami.com/v1/data/band_data.json'
	headers={
		'apptoken': auth_info['token_info']['app_token'],
	}
	data={
		'query_type': 'summary',
		'device_type': 'android_phone',
		'userid': auth_info['token_info']['user_id'],
		'from_date': '2023-07-01',
		'to_date': '2023-08-02',
	}
	response=requests.get(band_data_url,params=data,headers=headers)
	
	''' We need to calculare today's midnight so that we can assess whether
	a date entry relates to today or not.
	
	The idea being that we want to write points throughout the current day
	but for previous days only want to write/update the previous entry
	'''
	today = datetime.datetime.today()
	midnight = datetime.datetime.combine(today, datetime.datetime.min.time())
	today_ts = today.strftime('%s')
	
	for daydata in response.json()['data']:
		day = daydata['date_time']
		print(day)
		# Parse the date into a datetime
		day_ts = datetime.datetime.strptime(daydata['date_time'], '%Y-%m-%d')
		
		# If day_ts is < midnight we want to set the timestamp to be 23:59:59 
		# on that day. If not, then we use the current timestamp.
		print(day_ts)
		print(midnight)
		print(today_ts)
		if day_ts < midnight:
			ts = day_ts.strftime('%s')
		else:
			ts = today_ts
		
		summary=json.loads(base64.b64decode(daydata['summary']))
		for k,v in summary.items():
			if k=='stp':
				dump_step_data(day,v)
			elif k=='slp':
				# dump_sleep_data(day,v)
				# Extract the data
				result_set = result_set + extract_sleep_data(ts, v)
				print(result_set)
			else:
				print(k,"=",v)
				
	return result_set

def write_results(results):
    ''' Open a connection to InfluxDB and write the results in
    '''

    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as _client:
        with _client.write_api() as _write_client:
            # Iterate through the results generating and writing points
            for row in results:
                p = Point(INFLUXDB_MEASUREMENT)
                for tag in row['tags']:
                    p = p.tag(tag, row['tags'][tag])
                    
                for field in row['fields']:
                    p = p.field(field, row['fields'][field])
                    
                p = p.time(row['timestamp'])
                _write_client.write(INFLUXDB_BUCKET, p)

def main():
	
	# InfluxDB settings
	INFLUXDB_URL = os.getenv("INFLUXDB_URL", False)
	INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
	INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "")
	INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
	INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "testing_db")
	
	parser = argparse.ArgumentParser()
	parser.add_argument("--email",required=True,help="email address for login")
	parser.add_argument("--password",required=True,help="password for login")
	args=parser.parse_args()
	auth_info=mifit_auth_email(args.email,args.password)
	get_band_data(auth_info)


if __name__== "__main__":
	main()
