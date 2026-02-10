#!/usr/bin/env python3
"""
Bluesky Scheduled Bot - Date-Specific Posts Version
Supports both date-specific posts and regular rotating posts.
Designed for GitHub Actions.
"""

import os
import json
import sys
import random
from datetime import datetime
from atproto import Client
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BlueskyBot:
    def __init__(self, handle, password, scheduled_posts_file='scheduled_posts.json', 
                 regular_posts_file='posts.txt', state_file='bot_state.json'):
        """
        Initialize the Bluesky bot.
        
        Args:
            handle: Your Bluesky handle
            password: Your Bluesky app password
            scheduled_posts_file: JSON file with date-specific posts
            regular_posts_file: Text file with regular rotating posts
            state_file: Tracks progress through regular posts
        """
        self.handle = handle
        self.password = password
        self.scheduled_posts_file = scheduled_posts_file
        self.regular_posts_file = regular_posts_file
        self.state_file = state_file
        self.client = None
        
    def authenticate(self):
        """Authenticate with Bluesky."""
        try:
            self.client = Client()
            self.client.login(self.handle, self.password)
            logger.info(f"Successfully authenticated as {self.handle}")
            return True
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    def load_scheduled_posts(self):
        """Load date-specific scheduled posts from JSON file."""
        try:
            if not os.path.exists(self.scheduled_posts_file):
                logger.info(f"No scheduled posts file found: {self.scheduled_posts_file}")
                return []
            
            with open(self.scheduled_posts_file, 'r', encoding='utf-8') as f:
                posts = json.load(f)
            logger.info(f"Loaded {len(posts)} scheduled posts")
            return posts
        except Exception as e:
            logger.error(f"Error loading scheduled posts: {e}")
            return []
    
    def load_regular_posts(self):
        """Load regular rotating posts from text file."""
        try:
            if not os.path.exists(self.regular_posts_file):
                logger.warning(f"No regular posts file found: {self.regular_posts_file}")
                return []
                
            with open(self.regular_posts_file, 'r', encoding='utf-8') as f:
                posts = [line.strip() for line in f if line.strip()]
            logger.info(f"Loaded {len(posts)} regular posts")
            return posts
        except Exception as e:
            logger.error(f"Error loading regular posts: {e}")
            return []
    
    def load_state(self):
        """Load the bot state (recent posts history for avoiding repeats)."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                return state.get('recent_posts', [])
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
        return []
    
    def save_state(self, recent_posts):
        """
        Save the bot state (recent posts history).
        Args:
            recent_posts: List of recently used post texts (max 220 for 110 days at 2/day)
        """
        try:
            # Keep only the last 220 posts
            recent_posts = recent_posts[-220:]
            
            with open(self.state_file, 'w') as f:
                json.dump({'recent_posts': recent_posts}, f, indent=2)
            logger.info(f"Saved state: tracking {len(recent_posts)} recent posts")
        except Exception as e:
            logger.error(f"Could not save state: {e}")
    
    def reset_annual_posts_if_new_year(self):
        """
        On January 1st, reset all 'posted' flags to false for the new year.
        This allows annual posts (like holidays) to repeat each year.
        """
        today = datetime.now()
        
        # Only run this check on January 1st
        if today.month != 1 or today.day != 1:
            return
        
        try:
            scheduled_posts = self.load_scheduled_posts()
            reset_count = 0
            
            for post in scheduled_posts:
                if post.get('posted', False):
                    post['posted'] = False
                    # Remove the posted_at timestamp
                    if 'posted_at' in post:
                        del post['posted_at']
                    reset_count += 1
            
            if reset_count > 0:
                with open(self.scheduled_posts_file, 'w', encoding='utf-8') as f:
                    json.dump(scheduled_posts, f, indent=2, ensure_ascii=False)
                
                logger.info(f"🎉 New Year! Reset {reset_count} scheduled posts for annual repeat")
        except Exception as e:
            logger.error(f"Error resetting annual posts: {e}")
    
    def find_scheduled_post_for_today(self):
        """
        Find if there's a scheduled post for today.
        Returns the post text if found, None otherwise.
        Matches only on month and day (ignoring year) for annual repeating.
        """
        scheduled_posts = self.load_scheduled_posts()
        today = datetime.now()
        today_month_day = today.strftime('%m-%d')  # e.g., "12-25" for Dec 25
        
        for post in scheduled_posts:
            # Extract month-day from the post's date (ignore year)
            post_date = post.get('date', '')
            if len(post_date) >= 10:  # Format: YYYY-MM-DD
                post_month_day = post_date[5:]  # Extract MM-DD
                
                # Check if month and day match (ignoring year)
                if post_month_day == today_month_day:
                    # Check if this post has already been sent this year
                    if post.get('posted', False):
                        logger.info(f"Scheduled post for {today_month_day} was already sent this year")
                        continue
                    
                    logger.info(f"Found scheduled post for {today_month_day}")
                    return post
        
        return None
    
    def mark_scheduled_post_as_sent(self, post_month_day):
        """
        Mark a scheduled post as sent to prevent duplicate posting.
        Args:
            post_month_day: Month-day string in MM-DD format (e.g., "12-25")
        """
        try:
            scheduled_posts = self.load_scheduled_posts()
            
            for post in scheduled_posts:
                post_date = post.get('date', '')
                if len(post_date) >= 10:
                    # Extract MM-DD from YYYY-MM-DD
                    if post_date[5:] == post_month_day:
                        post['posted'] = True
                        post['posted_at'] = datetime.now().isoformat()
            
            with open(self.scheduled_posts_file, 'w', encoding='utf-8') as f:
                json.dump(scheduled_posts, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Marked scheduled post for {post_month_day} as sent")
        except Exception as e:
            logger.error(f"Could not mark scheduled post as sent: {e}")
    
    def post_next(self):
        """Post the next message (scheduled or regular)."""
        
        # First, check if it's Jan 1st and reset annual posts if needed
        self.reset_annual_posts_if_new_year()
        
        # Then, check if there's a scheduled post for today
        scheduled_post = self.find_scheduled_post_for_today()
        
        if scheduled_post:
            post_text = scheduled_post.get('text', '')
            post_date = scheduled_post.get('date', '')
            post_month_day = post_date[5:] if len(post_date) >= 10 else ''
            
            if not post_text:
                logger.error("Scheduled post has no text")
                return False
            
            try:
                if not self.authenticate():
                    return False
                
                self.client.send_post(text=post_text)
                logger.info(f"✅ Posted SCHEDULED post for {post_month_day}: {post_text[:100]}...")
                
                # Mark as sent
                self.mark_scheduled_post_as_sent(post_month_day)
                
                return True
                
            except Exception as e:
                logger.error(f"Error posting scheduled message: {e}")
                return False
        
        # No scheduled post for today, use regular rotation
        logger.info("No scheduled post for today, using regular rotation")
        
        regular_posts = self.load_regular_posts()
        
        if not regular_posts:
            logger.error("No regular posts available to send")
            return False
        
        # Load recent posts history
        recent_posts = self.load_state()
        
        # Get posts that haven't been used recently
        available_posts = [p for p in regular_posts if p not in recent_posts]
        
        # If all posts have been used recently, reset and use all posts
        if not available_posts:
            logger.info("All posts used recently - resetting available pool")
            available_posts = regular_posts
            recent_posts = []  # Clear history
        
        # Select a random post from available posts
        post_text = random.choice(available_posts)
        
        try:
            if not self.authenticate():
                return False
            
            self.client.send_post(text=post_text)
            logger.info(f"✅ Posted RANDOM post: {post_text[:100]}...")
            logger.info(f"   (Selected from {len(available_posts)} available, {len(recent_posts)} recent)")
            
            # Add to recent posts and save state
            recent_posts.append(post_text)
            self.save_state(recent_posts)
            
            return True
            
        except Exception as e:
            logger.error(f"Error posting regular message: {e}")
            return False


def main():
    """Main function to run the bot once."""
    
    # Get credentials from environment variables
    BLUESKY_HANDLE = os.getenv('BLUESKY_HANDLE')
    BLUESKY_PASSWORD = os.getenv('BLUESKY_PASSWORD')
    
    if not BLUESKY_HANDLE or not BLUESKY_PASSWORD:
        logger.error("Missing credentials! Set BLUESKY_HANDLE and BLUESKY_PASSWORD environment variables.")
        sys.exit(1)
    
    # Initialize and run bot
    bot = BlueskyBot(BLUESKY_HANDLE, BLUESKY_PASSWORD)
    
    success = bot.post_next()
    
    if success:
        logger.info("✅ Bot completed successfully")
        sys.exit(0)
    else:
        logger.error("❌ Bot failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
