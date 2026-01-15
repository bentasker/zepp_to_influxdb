# Zepp to InfluxDB

This script connects to the Zepp/MiFit/Huami/Amznfit smart watch API in order to retrieve step count and sleep information to then write onwards into an [InfluxDB](https://github.com/influxdata/influxdb) instance.

The underlying connectivity approach is taken from [Reverse engineering of the Mi Fit API](https://github.com/micw/hacking-mifit-api/blob/master/README.md) .

I put this script together in order to collect data from my Amznfit Bip 3 Pro, having found that [the Bip 3 didn't work with Gadgetbridge](https://projects.bentasker.co.uk/gils_projects/issue/jira-projects/MISC/34.html).

Design details can be found in [MISC#35](https://projects.bentasker.co.uk/gils_projects/issue/jira-projects/MISC/35.html).

The app seems to update values irregularly, so hourly runs should be more than sufficient.

Note: Bip 3 support has since [been added](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/3249#) to Gadgetbridge, so I've [changed approach](https://www.bentasker.co.uk/posts/blog/software-development/linking-a-bip3-smartwatch-with-gadgetbridge-to-write-stats-to-influxdb.html) and so am unlikely to add to this codebase any further.

----

### Configuration

The script is designed to run as a docker container and so takes its configuration from environment variables
    
- `INFLUXDB_URL`: the URL of the InfluxDB server to write to
- `INFLUXDB_TOKEN`: the token to authenticate against InfluxDB with (provide user:pass if it's a v1 instance)
- `INFLUXDB_ORG`: The org name or ID to write into
- `INFLUXDB_MEASUREMENT`: The measurement name to write into (default `zepp`)
- `INFLUXDB_BUCKET`: The bucket/database to write into (default `telegraf`)
- `QUERY_DURATION`: How many days data to fetch from the API (default 2)

**Authentication (choose one):**

*Option 1: Email/Password*
- `ZEPP_EMAIL`: The email address used to sign into Zepp/MiFit/Huami/Amznfit
- `ZEPP_PASS`: The password to your Zepp account

*Option 2: App Token (recommended)*
- `ZEPP_APP_TOKEN`: App token extracted from Zepp mobile app
- `ZEPP_USER_ID`: Your Zepp user ID

> **Why use Option 2?** The Zepp API has strict rate limits on the login endpoint. Using a pre-extracted token bypasses login entirely, avoiding 429 rate limit errors.

----

### Extracting App Token via MITM Proxy

To get your `app_token` and `user_id` from the Zepp mobile app, you can use a MITM (Man-in-the-Middle) proxy to intercept the API traffic.

#### Step 1: Install mitmproxy on your computer

```sh
# macOS
brew install mitmproxy

# Linux (Ubuntu/Debian)
sudo apt install mitmproxy
# or
pip install mitmproxy

# Windows
# Download from https://mitmproxy.org/
```

#### Step 2: Start the proxy

```sh
mitmproxy --listen-port 8080
```

Note your computer's local IP address (e.g., `192.168.1.100`).

#### Step 3: Configure your phone

<details>
<summary><strong>iPhone Instructions</strong></summary>

**Set up proxy:**
1. Connect iPhone to same WiFi as your computer
2. Settings → WiFi → tap (i) next to your network
3. Scroll down → Configure Proxy → Manual
4. Server: `YOUR_COMPUTER_IP` (e.g., 192.168.1.100)
5. Port: `8080`
6. Save

**Install certificate:**
1. Open Safari on iPhone
2. Go to: http://mitm.it
3. Tap "Apple" → Download profile
4. Settings → General → VPN & Device Management → mitmproxy → Install

**Trust certificate:**
1. Settings → General → About → Certificate Trust Settings
2. Enable toggle for mitmproxy

</details>

<details>
<summary><strong>Android Instructions</strong></summary>

**Set up proxy:**
1. Connect Android to same WiFi as your computer
2. Settings → WiFi → Long press your network → Modify network
3. Advanced options → Proxy → Manual
4. Proxy hostname: `YOUR_COMPUTER_IP` (e.g., 192.168.1.100)
5. Proxy port: `8080`
6. Save

**Install certificate:**
1. Open Chrome on Android
2. Go to: http://mitm.it
3. Tap "Android" → Download certificate
4. Settings → Security → Encryption & credentials → Install a certificate → CA certificate
5. Select the downloaded `mitmproxy-ca-cert.cer` file

> **Note:** On Android 7+, user-installed certificates are not trusted by apps by default. You may need a rooted device or use Android 6 or below for this to work with the Zepp app.

</details>

#### Step 4: Capture the token

1. Open Zepp app on your phone
2. Navigate around (sync data, view sleep, etc.)
3. Watch mitmproxy terminal for requests to `api-mifit.huami.com` or `api-mifit.zepp.com`
4. Press Enter on a request to see details
5. Find in the headers:
   - `apptoken` → use as `ZEPP_APP_TOKEN`
   - `userid` (in URL params) → use as `ZEPP_USER_ID`

#### Step 5: Cleanup (important!)

**iPhone:**
- Settings → WiFi → Configure Proxy → Off
- Settings → General → VPN & Device Management → mitmproxy → Remove Profile

**Android:**
- Settings → WiFi → Your network → Proxy → None
- Settings → Security → Encryption & credentials → Trusted credentials → User → Remove mitmproxy

#### Step 6: Use the extracted values

```sh
export ZEPP_APP_TOKEN="your_captured_apptoken"
export ZEPP_USER_ID="your_captured_userid"
```

Or in Docker:
```sh
docker run --rm \
-e ZEPP_APP_TOKEN="your_token" \
-e ZEPP_USER_ID="your_userid" \
-e INFLUXDB_URL=http://192.168.3.84:8086 \
...
```

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
