#!/usr/bin/env python3
import pygame
import RPi.GPIO as GPIO
import time
from oled import FaceCommand, Expression, OLEDFace, oled_process
import threading
import multiprocessing as mp
from multiprocessing import Process, Queue, Event, Manager
import json
import signal
import sys
import logging

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from enum import Enum


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RobotState(Enum):
    INIT = "init"
    READY = "ready"
    MOVING = "moving"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class RobotCommand:
    vx: float = 0.0
    vy: float = 0.0
    vr: float = 0.0
    emergency_stop: bool = False
    timestamp: float = 0.0


@dataclass
class RobotStatus:
    state: RobotState
    encoder_counts: List[int]
    motor_speeds: List[float]
    battery_voltage: float
    timestamp: float


# Motor and control classes remain the same as previous version
class MotorController:
    def __init__(self, motor_pins):
        self.motors = self._setup_motors(motor_pins)
        self.encoder_counts = [0, 0, 0, 0]
        self.encoder_lock = threading.Lock()
        self.encoder_pins = []
        
    def _setup_motors(self, motor_pins):
        motors = []
        for pins in motor_pins:
            motor = MotorDriver(pins['in1'], pins['in2'], pins['pwm'])
            motors.append(motor)
        return motors
    
    def setup_encoders(self, encoder_pins):
        self.encoder_pins = encoder_pins
        for i, pins in enumerate(encoder_pins):
            for pin in pins:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(pins[0], GPIO.BOTH, 
                                callback=lambda channel, idx=i: self.encoder_callback(channel, idx))
    
    def encoder_callback(self, channel, motor_index):
        with self.encoder_lock:
            a_state = GPIO.input(self.encoder_pins[motor_index][0])
            b_state = GPIO.input(self.encoder_pins[motor_index][1])
            
            if a_state == GPIO.HIGH:
                if b_state == GPIO.LOW:
                    self.encoder_counts[motor_index] += 1
                else:
                    self.encoder_counts[motor_index] -= 1
            else:
                if b_state == GPIO.HIGH:
                    self.encoder_counts[motor_index] += 1
                else:
                    self.encoder_counts[motor_index] -= 1
    
    def mecanum_drive(self, vx, vy, vr):
        wheel_speeds = [
            vy + vx + vr,  # Front Left
            vy - vx - vr,  # Front Right
            vy - vx + vr,  # Rear Left
            vy + vx - vr   # Rear Right
        ]
        
        max_speed = max(abs(speed) for speed in wheel_speeds)
        if max_speed > 100:
            wheel_speeds = [speed * 100 / max_speed for speed in wheel_speeds]
        
        for i, speed in enumerate(wheel_speeds):
            self.motors[i].set_speed(speed)
        
        return wheel_speeds
    
    def stop(self):
        for motor in self.motors:
            motor.set_speed(0)
    
    def get_encoder_data(self):
        with self.encoder_lock:
            return self.encoder_counts.copy()


class MotorDriver:
    def __init__(self, in1, in2, pwm):
        self.IN1 = in1
        self.IN2 = in2
        self.PWM = pwm
        
        GPIO.setup(self.IN1, GPIO.OUT)
        GPIO.setup(self.IN2, GPIO.OUT)
        GPIO.setup(self.PWM, GPIO.OUT)
        
        self.pwm_obj = GPIO.PWM(self.PWM, 1000)
        self.pwm_obj.start(0)
    
    def set_speed(self, speed):
        speed = max(-100, min(100, speed))
        
        if speed >= 0:
            GPIO.output(self.IN1, GPIO.HIGH)
            GPIO.output(self.IN2, GPIO.LOW)
        else:
            GPIO.output(self.IN1, GPIO.LOW)
            GPIO.output(self.IN2, GPIO.HIGH)
        
        self.pwm_obj.ChangeDutyCycle(abs(speed))


def control_process(command_queue: Queue, status_queue: Queue, face_command_queue: Queue, shutdown_event: Event):
    """Enhanced control process with facial expression feedback"""
    logger.info("Control process started")
    
    try:
        # Hardware configuration
        motor_pins = [
            {'in1': 17, 'in2': 18, 'pwm': 4},    # FL
            {'in1': 22, 'in2': 23, 'pwm': 24},   # FR
            {'in1': 10, 'in2': 9, 'pwm': 11},    # RL
            {'in1': 25, 'in2': 8, 'pwm': 7}      # RR
        ]
        
        encoder_pins = [
            [27, 13], [19, 16], [20, 21], [12, 6]
        ]
        
        # Setup GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        
        # Initialize motor controller
        motor_controller = MotorController(motor_pins)
        motor_controller.setup_encoders(encoder_pins)
        
        # Control variables
        control_rate = 50  # Hz
        control_interval = 1.0 / control_rate
        
        last_status_time = time.time()
        status_interval = 0.1  # 10 Hz status updates
        
        last_movement_time = time.time()
        is_moving = False
        
        while not shutdown_event.is_set():
            loop_start = time.time()
            
            # Process incoming commands
            current_command = None
            while not command_queue.empty():
                current_command = command_queue.get_nowait()
            
            # Execute motor command and update facial expressions
            if current_command:
                if current_command.emergency_stop:
                    motor_controller.stop()
                    face_command_queue.put(FaceCommand(Expression.SURPRISED, duration=2.0, priority=10))
                    logger.warning("Emergency stop activated")
                else:
                    motor_speeds = motor_controller.mecanum_drive(
                        current_command.vx, current_command.vy, current_command.vr
                    )
                    
                    # Change expression based on movement
                    if any(abs(speed) > 10 for speed in motor_speeds):
                        if not is_moving:
                            face_command_queue.put(FaceCommand(Expression.HAPPY, priority=5))
                            is_moving = True
                        last_movement_time = time.time()
                    else:
                        if is_moving and (time.time() - last_movement_time > 2.0):
                            face_command_queue.put(FaceCommand(Expression.NEUTRAL, priority=5))
                            is_moving = False
            
            # Send status updates
            current_time = time.time()
            if current_time - last_status_time >= status_interval:
                encoder_data = motor_controller.get_encoder_data()
                status = RobotStatus(
                    state=RobotState.MOVING if current_command and is_moving else RobotState.READY,
                    encoder_counts=encoder_data,
                    motor_speeds=motor_speeds if current_command else [0, 0, 0, 0],
                    battery_voltage=12.0,
                    timestamp=current_time
                )
                
                if not status_queue.full():
                    status_queue.put_nowait(status)
                
                last_status_time = current_time
            
            # Maintain control rate
            loop_time = time.time() - loop_start
            if loop_time < control_interval:
                time.sleep(control_interval - loop_time)
                
    except Exception as e:
        logger.error(f"Control process error: {e}")
        face_command_queue.put(FaceCommand(Expression.SAD, duration=3.0, priority=10))
    finally:
        motor_controller.stop()
        GPIO.cleanup()
        logger.info("Control process stopped")


def input_process(command_queue: Queue, face_command_queue: Queue, shutdown_event: Event):
    """Enhanced input process with facial expression triggers"""
    logger.info("Input process started")
    
    try:
        pygame.init()
        pygame.joystick.init()
        
        if pygame.joystick.get_count() == 0:
            logger.error("No controller found!")
            face_command_queue.put(FaceCommand(Expression.SAD, priority=5))
            return
        
        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        logger.info(f"Controller: {joystick.get_name()}")
        face_command_queue.put(FaceCommand(Expression.HAPPY, duration=2.0, priority=5))
        
        input_rate = 30  # Hz
        input_interval = 1.0 / input_rate
        
        deadzone = 0.1
        last_button_time = 0
        button_cooldown = 0.5
        
        while not shutdown_event.is_set():
            loop_start = time.time()
            
            # Process pygame events
            for event in pygame.event.get():
                if event.type == pygame.JOYBUTTONDOWN:
                    current_time = time.time()
                    if current_time - last_button_time > button_cooldown:
                        # Trigger special expressions for buttons
                        if event.button == 0:  # A button
                            face_command_queue.put(FaceCommand(Expression.HAPPY, duration=1.5, priority=7))
                        elif event.button == 1:  # B button
                            face_command_queue.put(FaceCommand(Expression.SURPRISED, duration=1.0, priority=7))
                        elif event.button == 2:  # X button
                            face_command_queue.put(FaceCommand(Expression.WINK_LEFT, duration=1.0, priority=7))
                        elif event.button == 3:  # Y button
                            face_command_queue.put(FaceCommand(Expression.WINK_RIGHT, duration=1.0, priority=7))
                        elif event.button == 7:  # Xbox button
                            logger.info("Xbox button pressed - initiating shutdown")
                            face_command_queue.put(FaceCommand(Expression.SLEEPY, duration=2.0, priority=10))
                            shutdown_event.set()
                            break
                        last_button_time = current_time
            
            # Read joystick axes
            left_x = joystick.get_axis(0)
            left_y = -joystick.get_axis(1)  # Invert Y
            right_x = joystick.get_axis(2)
            
            # Apply deadzone
            left_x = left_x if abs(left_x) > deadzone else 0.0
            left_y = left_y if abs(left_y) > deadzone else 0.0
            right_x = right_x if abs(right_x) > deadzone else 0.0
            
            # Create command
            command = RobotCommand(
                vx=left_y * 100,
                vy=left_x * 100,
                vr=right_x * 100,
                timestamp=time.time()
            )
            
            # Send command to control process
            if not command_queue.full():
                command_queue.put_nowait(command)
            
            # Maintain input rate
            loop_time = time.time() - loop_start
            if loop_time < input_interval:
                time.sleep(input_interval - loop_time)
                
    except Exception as e:
        logger.error(f"Input process error: {e}")
    finally:
        pygame.quit()
        logger.info("Input process stopped")

def status_process(status_queue: Queue, shutdown_event: Event):
    """Status monitoring process"""
    logger.info("Status process started")
    
    status_rate = 5  # Hz
    status_interval = 1.0 / status_rate
    
    last_encoder_counts = [0, 0, 0, 0]
    last_print_time = time.time()
    print_interval = 2.0
    
    try:
        while not shutdown_event.is_set():
            loop_start = time.time()
            
            # Process status updates
            while not status_queue.empty():
                status = status_queue.get_nowait()
                
                # Calculate encoder deltas
                encoder_deltas = [current - last for current, last in 
                                zip(status.encoder_counts, last_encoder_counts)]
                last_encoder_counts = status.encoder_counts.copy()
                
                # Print status periodically
                current_time = time.time()
                if current_time - last_print_time >= print_interval:
                    logger.info(
                        f"State: {status.state.value}, "
                        f"Encoders: {status.encoder_counts}, "
                        f"Delta: {encoder_deltas}, "
                        f"Battery: {status.battery_voltage:.1f}V"
                    )
                    last_print_time = current_time
            
            # Maintain status rate
            loop_time = time.time() - loop_start
            if loop_time < status_interval:
                time.sleep(status_interval - loop_time)
                
    except Exception as e:
        logger.error(f"Status process error: {e}")
    finally:
        logger.info("Status process stopped")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)

def main():
    """Main function - orchestrates all processes"""
    logger.info("Starting Mecanum Robot System with OLED Face")
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create inter-process communication
    command_queue = Queue(maxsize=10)
    status_queue = Queue(maxsize=20)
    face_command_queue = Queue(maxsize=10)
    shutdown_event = Event()
    
    processes = []
    
    try:
        # Start OLED face process
        oled_proc = Process(
            target=oled_process,
            args=(face_command_queue, shutdown_event),
            name="OLEDProcess"
        )
        processes.append(oled_proc)
        
        # Start control process
        control_proc = Process(
            target=control_process,
            args=(command_queue, status_queue, face_command_queue, shutdown_event),
            name="ControlProcess"
        )
        processes.append(control_proc)
        
        # Start input process
        input_proc = Process(
            target=input_process,
            args=(command_queue, face_command_queue, shutdown_event),
            name="InputProcess"
        )
        processes.append(input_proc)
        
        # Start status process
        status_proc = Process(
            target=status_process,
            args=(status_queue, shutdown_event),
            name="StatusProcess"
        )
        processes.append(status_proc)
        
        # Start all processes
        for proc in processes:
            proc.start()
            logger.info(f"Started {proc.name} (PID: {proc.pid})")
        
        # Monitor processes
        while not shutdown_event.is_set():
            time.sleep(1)
            
            # Check if any process died
            for proc in processes:
                if not proc.is_alive():
                    logger.error(f"Process {proc.name} died unexpectedly")
                    shutdown_event.set()
                    break
        
    except Exception as e:
        logger.error(f"Main process error: {e}")
    finally:
        # Clean shutdown
        logger.info("Initiating shutdown sequence...")
        shutdown_event.set()
        
        # Wait for processes to terminate
        for proc in processes:
            proc.join(timeout=5.0)
            if proc.is_alive():
                logger.warning(f"Force terminating {proc.name}")
                proc.terminate()
        
        logger.info("Mecanum Robot System stopped")

if __name__ == "__main__":
    main()