import time
import psutil
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

device = ssd1306(i2c(port=1, address=0x3C), width=128, height=64)

def cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000

while True:
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/host").percent
    with canvas(device) as draw:
        draw.text((0, 0),  f"CPU  {psutil.cpu_percent():5.1f}%", fill="white")
        draw.text((0, 14), f"Temp {cpu_temp():5.1f}C", fill="white")
        draw.text((0, 28), f"Mem  {mem:5.1f}%", fill="white")
        draw.text((0, 42), f"Disk {disk:5.1f}%", fill="white")
    time.sleep(2)
