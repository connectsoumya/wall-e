#!/usr/bin/env python3
import pygame
import RPi.GPIO as GPIO
import time
import math
import threading
import multiprocessing as mp
from multiprocessing import Process, Queue, Event, Manager
import json
import signal
import sys
import logging
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from enum import Enum
import board
import busio
import adafruit_ssd1306
from PIL import Image, ImageDraw, ImageFont
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Expression(Enum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    BLINK = "blink"
    SLEEPY = "sleepy"
    WINK_LEFT = "wink_left"
    WINK_RIGHT = "wink_right"

@dataclass
class FaceCommand:
    expression: Expression
    duration: Optional[float] = None  # None = until next command
    priority: int = 1  # Higher priority overrides lower


class OLEDFace:
    def __init__(self, i2c_addresses: List[int] = [0x3C, 0x3D, 0x3E]):
        """Initialize OLED displays for eyes and mouth"""
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.displays = []
        self.display_types = []  # 0: left_eye, 1: right_eye, 2: mouth
        
        try:
            for i, addr in enumerate(i2c_addresses):
                try:
                    oled = adafruit_ssd1306.SSD1306_I2C(128, 64, self.i2c, addr=addr)
                    oled.fill(0)
                    oled.show()
                    self.displays.append(oled)
                    
                    if i < 2:
                        self.display_types.append("eye")
                    else:
                        self.display_types.append("mouth")
                        
                    logger.info(f"OLED display {i} initialized at address 0x{addr:02X}")
                except Exception as e:
                    logger.error(f"Failed to initialize OLED at 0x{addr:02X}: {e}")
                    self.displays.append(None)
                    self.display_types.append("unknown")
        
        except Exception as e:
            logger.error(f"Failed to initialize I2C for OLED: {e}")
            self.displays = [None, None, None]
            self.display_types = ["eye", "eye", "mouth"]
        
        # Expression definitions
        self.expressions = {
            Expression.NEUTRAL: self._draw_neutral,
            Expression.HAPPY: self._draw_happy,
            Expression.SAD: self._draw_sad,
            Expression.ANGRY: self._draw_angry,
            Expression.SURPRISED: self._draw_surprised,
            Expression.BLINK: self._draw_blink,
            Expression.SLEEPY: self._draw_sleepy,
            Expression.WINK_LEFT: self._draw_wink_left,
            Expression.WINK_RIGHT: self._draw_wink_right,
        }
        
        self.current_expression = Expression.NEUTRAL
        
    def _create_canvas(self):
        """Create a new drawing canvas"""
        return Image.new("1", (128, 64)), ImageDraw.Draw(Image.new("1", (128, 64)))
    
    def _draw_eye(self, draw, x: int, y: int, state: str = "open"):
        """Draw an eye at position (x,y)"""
        if state == "open":
            # Outer circle
            draw.ellipse([x-20, y-15, x+20, y+15], outline=1, fill=0)
            # Iris
            draw.ellipse([x-8, y-8, x+8, y+8], outline=1, fill=1)
            # Pupil
            draw.ellipse([x-4, y-4, x+4, y+4], outline=1, fill=0)
        elif state == "closed":
            # Closed eye (line)
            draw.line([x-20, y, x+20, y], fill=1, width=3)
        elif state == "wink":
            # Wink (half closed)
            draw.ellipse([x-20, y-8, x+20, y+8], outline=1, fill=0)
            draw.line([x-20, y, x+20, y], fill=1, width=2)
        elif state == "angry":
            # Angry eye (slanted)
            draw.ellipse([x-18, y-12, x+18, y+12], outline=1, fill=0)
            draw.line([x-15, y-10, x+15, y+8], fill=1, width=3)
        elif state == "sad":
            # Sad eye (droopy)
            draw.ellipse([x-18, y-10, x+18, y+14], outline=1, fill=0)
            draw.ellipse([x-6, y-2, x+6, y+6], outline=1, fill=1)
    
    def _draw_mouth(self, draw, expression: str):
        """Draw mouth based on expression"""
        if expression == "neutral":
            draw.line([30, 45, 98, 45], fill=1, width=2)
        elif expression == "happy":
            draw.arc([30, 30, 98, 60], 0, 180, fill=1, width=3)
        elif expression == "sad":
            draw.arc([30, 40, 98, 70], 180, 360, fill=1, width=3)
        elif expression == "angry":
            draw.arc([30, 35, 98, 55], 180, 360, fill=1, width=3)
            # Angry lines
            for x in [25, 103]:
                draw.line([x, 35, x+10, 25], fill=1, width=2)
        elif expression == "surprised":
            draw.ellipse([40, 35, 88, 55], outline=1, fill=0)
        elif expression == "open":
            draw.ellipse([35, 30, 93, 60], outline=1, fill=0)
    
    def _draw_neutral(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                self._draw_eye(draw, x, 32, "open")
            elif display_type == "mouth":
                self._draw_mouth(draw, "neutral")
            
            images.append(image)
        return images
    
    def _draw_happy(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                # Happy eyes are slightly closed
                draw.ellipse([x-15, 25, x+15, 35], outline=1, fill=0)
            elif display_type == "mouth":
                self._draw_mouth(draw, "happy")
            
            images.append(image)
        return images
    
    def _draw_sad(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                self._draw_eye(draw, x, 32, "sad")
            elif display_type == "mouth":
                self._draw_mouth(draw, "sad")
            
            images.append(image)
        return images
    
    def _draw_angry(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                self._draw_eye(draw, x, 32, "angry")
            elif display_type == "mouth":
                self._draw_mouth(draw, "angry")
            
            images.append(image)
        return images
    
    def _draw_surprised(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                # Wide open eyes
                draw.ellipse([x-18, 20, x+18, 44], outline=1, fill=0)
                draw.ellipse([x-6, 28, x+6, 36], outline=1, fill=1)
            elif display_type == "mouth":
                self._draw_mouth(draw, "surprised")
            
            images.append(image)
        return images
    
    def _draw_blink(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                self._draw_eye(draw, x, 32, "closed")
            elif display_type == "mouth":
                self._draw_mouth(draw, "neutral")
            
            images.append(image)
        return images
    
    def _draw_sleepy(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                x = 40 if is_left else 88
                # Sleepy eyes (zzz)
                draw.line([x-15, 32, x-5, 32], fill=1, width=2)
                draw.line([x, 30, x+10, 30], fill=1, width=2)
                draw.line([x+5, 34, x+15, 34], fill=1, width=2)
            elif display_type == "mouth":
                # Sleepy mouth (snooze)
                self._draw_mouth(draw, "neutral")
                draw.text((50, 50), "Zzz", fill=1)
            
            images.append(image)
        return images
    
    def _draw_wink_left(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                if is_left:
                    self._draw_eye(draw, 40, 32, "closed")  # Left eye closed
                else:
                    self._draw_eye(draw, 88, 32, "open")    # Right eye open
            elif display_type == "mouth":
                self._draw_mouth(draw, "happy")  # Smile for wink
            
            images.append(image)
        return images
    
    def _draw_wink_right(self):
        images = []
        for i, display_type in enumerate(self.display_types):
            image = Image.new("1", (128, 64))
            draw = ImageDraw.Draw(image)
            
            if display_type == "eye":
                is_left = (i == 0)
                if is_left:
                    self._draw_eye(draw, 40, 32, "open")    # Left eye open
                else:
                    self._draw_eye(draw, 88, 32, "closed")  # Right eye closed
            elif display_type == "mouth":
                self._draw_mouth(draw, "happy")  # Smile for wink
            
            images.append(image)
        return images
    
    def set_expression(self, expression: Expression):
        """Set facial expression"""
        if expression not in self.expressions:
            logger.warning(f"Unknown expression: {expression}")
            return False
        
        try:
            images = self.expressions[expression]()
            for i, display in enumerate(self.displays):
                if display is not None and i < len(images):
                    display.image(images[i])
                    display.show()
            
            self.current_expression = expression
            logger.debug(f"Expression changed to: {expression}")
            return True
        except Exception as e:
            logger.error(f"Failed to set expression {expression}: {e}")
            return False
    
    def clear(self):
        """Clear all displays"""
        for display in self.displays:
            if display is not None:
                display.fill(0)
                display.show()


def oled_process(face_command_queue: Queue, shutdown_event: Event):
    """OLED Face animation process with random expressions"""
    logger.info("OLED Face process started")
    
    try:
        face = OLEDFace()
        
        # Initial expression
        face.set_expression(Expression.NEUTRAL)
        
        # Expression probabilities and timing
        expressions = [
            (Expression.NEUTRAL, 0.4, 3.0, 5.0),
            (Expression.HAPPY, 0.2, 2.0, 4.0),
            (Expression.BLINK, 0.15, 0.2, 0.3),
            (Expression.SURPRISED, 0.1, 1.0, 2.0),
            (Expression.WINK_LEFT, 0.05, 0.5, 1.0),
            (Expression.WINK_RIGHT, 0.05, 0.5, 1.0),
            (Expression.SLEEPY, 0.05, 2.0, 3.0),
        ]
        
        last_expression_change = time.time()
        next_change_interval = random.uniform(3.0, 8.0)  # Random interval between expressions
        
        while not shutdown_event.is_set():
            current_time = time.time()
            
            # Check for incoming face commands (high priority)
            current_face_command = None
            while not face_command_queue.empty():
                current_face_command = face_command_queue.get_nowait()
            
            if current_face_command:
                # Execute command immediately
                face.set_expression(current_face_command.expression)
                if current_face_command.duration:
                    time.sleep(current_face_command.duration)
                    # Return to previous or random expression after command duration
                    if face_command_queue.empty():
                        face.set_expression(Expression.NEUTRAL)
                last_expression_change = current_time
                next_change_interval = random.uniform(3.0, 8.0)
            
            # Random expression changes (if no command)
            elif current_time - last_expression_change >= next_change_interval:
                # Weighted random selection
                rand_val = random.random()
                cumulative_prob = 0.0
                selected_expr = Expression.NEUTRAL
                min_dur, max_dur = 3.0, 5.0
                
                for expr, prob, min_d, max_d in expressions:
                    cumulative_prob += prob
                    if rand_val <= cumulative_prob:
                        selected_expr = expr
                        min_dur, max_dur = min_d, max_d
                        break
                
                face.set_expression(selected_expr)
                last_expression_change = current_time
                next_change_interval = random.uniform(min_dur, max_dur)
                
                logger.debug(f"Random expression: {selected_expr} for {next_change_interval:.1f}s")
            
            time.sleep(0.1)  # 10Hz update rate
            
    except Exception as e:
        logger.error(f"OLED process error: {e}")
    finally:
        try:
            face.clear()
        except:
            pass
        logger.info("OLED Face process stopped")