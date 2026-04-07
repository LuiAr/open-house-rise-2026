# Ball-Following Robot - Open House Demo

## What this does

The laptop runs the AI that detects a yellow tennis ball and tells the robot where to go. You control it through a webpage.

---

## Starting the demo

**Step 1** - Make sure the OBSBOT camera is plugged into the Raspberry Pi.

**Step 2** - Open a Terminal on the laptop and run:

```
python laptop_server.py
```

**Step 3** - On the Raspberry Pi, run:

```
python3 <PATH_TO_FILE.py>
```

**Step 4** - Open the web browser on the laptop and go to:

```
http://localhost:5050
```

Thats where the dashboard with the camera feed and the controls are.

---

## Using the dashboard

- The **green dot** in the top bar means the AI server is running.
- The camera feed shows what the robot sees. A coloured box appears around the tennis ball when detected.
- Click **⚙ Settings** (top right) to open/close controls.

### To make the robot follow the ball

1. Open Settings.
2. Turn on **Robot enabled** (toggle in the Robot Movement section).
3. Place the yellow tennis ball in front of the camera and the robot will start moving.

### Scared mode

When turned on, the robot reverses when a mobile phone (or any other object added to the list) is detected by the camera. Enable it in Settings under "Scared Mode".

---

### **Note** - running everything on the laptop (for testing without the Pi):

> Open two Terminal windows. \
> In the first one -> run `python laptop_server.py`. \
> In the second one -> run `python app_split.py`. \
> Then open `http://localhost:5050`.
