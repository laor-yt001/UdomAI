import facebook_util
import os
from dotenv import load_dotenv

load_dotenv()

PAGE_ID = os.environ.get('FB_PAGE_ID')
TOKEN = os.environ.get('FB_TOKEN')

if __name__ == "__main__":
    print("=== Facebook Custom Post Generator ===")
    movie_title = input("Enter movie title (e.g., រៀបអភិសេកបង្ខិតបង្ខំអោយមានស្នេហ៍): ")
    episode = input("Enter episode number (e.g., 22): ")
    video_path = input("Enter video file path (e.g., test.mp4): ")

    message = f"""🎥 រឿង៖ « {movie_title} » ភាគទី{episode}
🎙 បកប្រែ និងបញ្ចូលសំឡេងដោយ៖ « Live ALONE & AI KHMER MOVIE »
សូមរីករាយទស្សនាដោយមេត្រី!
#fypシ #viralreelschallenge #fypシ゚viralシ"""

    if os.path.exists(video_path):
        print(f"\nPosting video '{video_path}' with the following caption:\n")
        print(message)
        print("-" * 40)
        success, res = facebook_util.post_to_facebook(PAGE_ID, TOKEN, message=message, media_path=video_path)
        if success:
            print(f"Successfully posted! Post ID: {res}")
        else:
            print(f"Failed to post. Error: {res}")
    else:
        print(f"Video file not found at '{video_path}'. Please provide the correct video file path.")
