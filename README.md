# Zepp to InfluxDB

This script connects to the Zepp/MiFit/Huami/Amznfit smart watch API in order to retrieve step count and sleep information to then write onwards into an [InfluxDB](https://github.com/influxdata/influxdb) instance.

The underlying connectivity approach is taken from [Reverse engineering of the Mi Fit API](https://github.com/micw/hacking-mifit-api/blob/master/README.md) .

I put this script together in order to collect data from my Amznfit Bip 3 Pro, having found that [the Bip 3 doesn't work with Gadgetbridge](https://projects.bentasker.co.uk/gils_projects/issue/jira-projects/MISC/34.html).

Design details can be found in [MISC#35](https://projects.bentasker.co.uk/gils_projects/issue/jira-projects/MISC/35.html).

The app seems to update values irregularly, so hourly runs should be more than sufficient.

----

### Configuration

The script is designed to run as a docker container and so takes its configuration from environment variables
    
- `INFLUXDB_URL`: the URL of the InfluxDB server to write to
- `INFLUXDB_TOKEN`: the token to authenticate against InfluxDB with (provide user:pass if it's a v1 instance)
- `INFLUXDB_ORG`: The org name or ID to write into
- `INFLUXDB_MEASUREMENT`: The measurement name to write into (default `zepp`)
- `INFLUXDB_BUCKET`: The bucket/database to write into (default `telegraf`)
- `QUERY_DURATION`: How many days data to fetch from the API (default 2)
- `ZEPP_EMAIL`: The email address used to sign into Zepp/MiFit/Huami/Amznfit
- `ZEPP_PASS`: The password to your Zepp account


----

### Running

The simplest way to run the script is via docker container

```sh
docker run --rm \
-e INFLUXDB_URL=http://192.168.3.84:8086 \
-e INFLUXDB_TOKEN=ffffaaaaaccccc \
-e INFLUXDB_ORG=abcdef \
-e ZEPP_PASS=mypass \
-e ZEPP_EMAIL=someone@example.invalud \
bentasker12/zepp_to_influxdb:latest
```

To run without a container, first install dependencies
```sh
pip install -r requirements.txt
```

Export the necessary variables into the environment, and then trigger the script
```sh
./app/mifit_to_influxdb.py
```

`cron` can be used to schedule runs if the container or the script are being directly called.

----

### Scheduling with Kubernetes

Kubernete's `CronJob` type can be used to schedule runs of the container. 

As a matter of good practice, secrets should be stored as secrets rather than being written into the job spec:
```sh
kubectl create secret generic zepp \
--from-literal='email=someone@example.invalid' \
--from-literal='pass=MyPass'

kubectl create secret generic influxdbv1 \
--from-literal=influxdb_token='ffffaaaaaccccc' \
--from-literal=influxdb_org='abcdef' \
--from-literal=influxdb_url='http://192.168.3.84:8086'
```

A `CronJob` spec can then be created drawing from these secrets
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: zepp-to-influxdb
spec:
  schedule: "30 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: zepp-to-influxdb
            image: bentasker12/zepp_to_influxdb:latest
            imagePullPolicy: IfNotPresent
            env:
            - name: INFLUXDB_BUCKET
              value: "telegraf"
            - name: INFLUXDB_MEASUREMENT
              value: "zepp"
            - name: QUERY_DURATION
              value: "2"
              
            - name: INFLUXDB_TOKEN
              valueFrom: 
                 secretKeyRef:
                    name: influxdbv1
                    key: influxdb_token
            - name: INFLUXDB_ORG
              valueFrom: 
                 secretKeyRef:
                    name: influxdbv1
                    key: influxdb_org
            - name: INFLUXDB_URL
              valueFrom: 
                 secretKeyRef:
                    name: influxdbv1
                    key: influxdb_url
            - name: ZEPP_EMAIL
              valueFrom: 
                 secretKeyRef:
                    name: zepp
                    key: email
            - name: ZEPP_PASS
              valueFrom: 
                 secretKeyRef:
                    name: zepp
                    key: pass
                    
          restartPolicy: OnFailure
```

Applying the spec
```
kubectl apply -f zepp_cron.yml
```

----

### License

Copyright (c) 2023 B Tasker, Michael Wyraz
Released under BSD 3-Clause (see script header)
