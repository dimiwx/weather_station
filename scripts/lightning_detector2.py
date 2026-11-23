import sys
import time
import csv
import subprocess
from datetime import datetime
from DFRobot_AS3935_Lib import DFRobot_AS3935
import RPi.GPIO as GPIO
import threading # We will use threading to handle events

# I2C and Pin Configuration
AS3935_I2C_ADDR3 = 0X03
AS3935_CAPACITANCE = 80
IRQ_PIN = 11
HEALTH_CHECK_S = 600  # confirm every 10 min that the sensor still answers on I2C

# --- Global flag to signal an event from the interrupt ---
# We use a threading Event which is safer than a simple boolean flag
lightning_event = threading.Event()

def handle_lightning_event():
    """
    This function contains all the slow actions (I/O) and is run
    when the main loop detects the event flag has been set.
    """
    global sensor
    
    # Now we can safely access the sensor outside the interrupt
    intSrc = sensor.get_interrupt_src()

    if intSrc == 1:
        lightning_distKm = sensor.get_lightning_distKm()
        lightning_energy_val = sensor.get_strike_energy_raw()
        now = datetime.utcnow()
        timestamp_csv = now.strftime("%Y-%m-%d %H:%M:%S")

        print('--- Lightning Detected! ---')
        print(f'Timestamp: {timestamp_csv}')
        print(f'Distance: {lightning_distKm}km')
        print(f'Intensity: {lightning_energy_val}')
        
        # Append data to the CSV file
        try:
            csv_file = "/home/dimitris/weather_station/weather_station_data.csv"
            row = [timestamp_csv, "", "", "", "", "Lightning is Detected", lightning_distKm]
            with open(csv_file, mode="a", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(row)
            print("Data logged to CSV.")
        except Exception as e:
            print(f"Failed to write to CSV: {e}")
        
        # Capture image
        try:
            timestamp_img = now.strftime("%Y%m%d_%H%M%S")
            img_filename = f"/home/dimitris/weather_station/whole_sky_camera/THUNDER_{timestamp_img}.jpg"
            bash_cmd = f"libcamera-still -n --immediate --hdr --denoise cdn_off --autofocus-mode auto --lens-position 0.05 --shutter 500000 --gain 1 --width 1920 --height 1080 --nopreview -o {img_filename}"
            subprocess.run(bash_cmd, shell=True, check=True)
            print("Image captured and saved as:", img_filename)
        except subprocess.CalledProcessError as e:
            print(f"Failed to capture image: {e}")
            
    elif intSrc == 2:
        print('Disturber discovered!')
    elif intSrc == 3:
        print('Noise level too high!')

def sensor_answers(tries=3, pause_s=1.0):
    """
    Reads register 0x00 (AFE gain). Reading 0x03 instead would clear a pending interrupt.
    Its top two bits are always 0 on a working AS3935, so 0xFF means a dead or floating bus.
    Tries up to `tries` times, `pause_s` apart: I2C failures come in bursts of ~0.6 s, so one
    failed read doesn't mean the sensor is gone.
    """
    for attempt in range(tries):
        try:
            if (sensor.i2cbus.read_byte_data(AS3935_I2C_ADDR3, 0x00) & 0xC0) == 0:
                return True
        except OSError:
            pass
        if attempt < tries - 1:
            time.sleep(pause_s)
    return False

def interrupt_callback(channel):
    """
    This is our new interrupt handler. It is VERY FAST.
    All it does is set an event flag and return immediately.
    """
    # Don't do I2C communication or any slow tasks here!
    lightning_event.set()

# --- Main script execution ---
GPIO.setmode(GPIO.BOARD)
sensor = DFRobot_AS3935(AS3935_I2C_ADDR3, bus=1)

if not sensor.reset():
    print("Init sensor fail. Please check connection.")
    sys.exit(1)
print("Init sensor success.")

# Configure sensor
sensor.power_up()
sensor.set_outdoors()
sensor.disturber_en()
sensor.set_tuning_caps(AS3935_CAPACITANCE)
sensor.set_noise_floor_lv1(3)
sensor.set_watchdog_threshold(3)
sensor.set_spike_rejection(3)
print("Sensor configured.")

# Setup GPIO interrupt
GPIO.setup(IRQ_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.add_event_detect(IRQ_PIN, GPIO.RISING, callback=interrupt_callback, bouncetime=10) # bouncetime helps ignore noise
print("GPIO interrupt attached. Waiting for lightning...")

try:
    # --- Main loop ---
    # The script will live here, waiting for the event flag.
    # If the sensor stops answering after startup, no interrupts ever arrive and the script would
    # wait silently forever, so check it regularly and exit: auto_lightning.sh then restarts it,
    # which re-initializes the sensor (or keeps reporting "Init sensor fail" until it is back).
    last_health_check = time.monotonic()
    while True:
        if time.monotonic() - last_health_check > HEALTH_CHECK_S:
            last_health_check = time.monotonic()
            if not sensor_answers():
                print(f"[{datetime.utcnow().isoformat()}] AS3935 stopped answering on I2C. Exiting so it is re-initialized.")
                sys.exit(1)

        # wait() will pause the loop here until lightning_event.set() is called
        if lightning_event.wait(timeout=1.0): # Timeout allows checking every 1s
            try:
                handle_lightning_event()
            except (OSError, TimeoutError) as e:
                # If an I2C error happens here, we just log it and continue
                print(f"[{datetime.utcnow().isoformat()}] I2C Error during event handling: {e}. Recovering.")
            
            lightning_event.clear() # Reset the flag for the next event

except KeyboardInterrupt:
    print("Program terminated by user.")
finally:
    GPIO.cleanup()
    print("GPIO cleaned up.")
