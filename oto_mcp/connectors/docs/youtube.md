## prerequisite — authorize YouTube on your Google account

from this card, click **connect**: Google asks you to authorize **YouTube only** (manage your channel, read its Analytics) on the account you choose. the Google account itself (address, token) is carried by the **Google account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- **one channel per connection**: if the Google account owns several channels (brand accounts), Google asks which one during consent — the tools act on that one; `youtube op=channel` tells which. Another channel = connect again and pick it
- the tools act **as the channel owner**: uploads, edits, thumbnails, replies and moderation are visible on YouTube
- an uploaded video is **private** unless you ask otherwise (or scheduled with a publication date); files up to 100 MB go straight through; a **project file of any size** (a video of several GB, put in the project with `oto_upload_url target=project_file large=true`) is uploaded **in the background** — the answer gives a `job_id`, `youtube op=job_status job_id=…` follows it until the `video_id`, and it resumes by itself after an interruption; the platform can upload 100 videos a day in all
- custom thumbnails need a **verified channel** (youtube.com/verify)
- the YouTube API quota belongs to the platform's Google app (shared, resets at midnight Pacific time): reads cost little, each write more — avoid bulk edits in a loop
- not included: captions, community posts, live streams
- several Google accounts: each tool acts on the one the call names with `_account=<email>` (the tools' `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; `google_accounts` tells which ones have authorized YouTube

## usage — run your channel like in Studio

`youtube` reads (channel, videos, video, playlists, comments, analytics, job_status), `youtube_act` changes (upload_video, update_video with `dry_run`, set_thumbnail, reply, set_moderation, delete_comment, add_to_playlist).
- "which of my videos got the most views over the last 28 days?"
- "upload this video from the project as a private draft titled X, then set this image as its thumbnail"
- "upload the 8 GB film I put in the project to my channel, unlisted, and tell me when it's done"
- "set this image as the thumbnail of my latest video"
- "show me the comments waiting for review and draft replies"
- "rewrite the description of video X — show me the diff first"
