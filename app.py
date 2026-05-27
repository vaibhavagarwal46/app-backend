import os
from datetime import datetime, timezone
from bson import ObjectId
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from pymongo.errors import ConfigurationError

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
app.config['JSON_MAX_BODY_SIZE'] = 32 * 1024 * 1024
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
jwt = JWTManager(app)

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", transports=['websocket'])

# Get the URI from environment variables
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("CRITICAL ERROR: MONGO_URI environment variable is not set!")
    exit(1) 

client = MongoClient(MONGO_URI)

# Simply hardcode the database name. Delete get_database() entirely.
db = client["neighborhood"]

# Configure Gemini AI
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Collections
users_collection = db.users
posts_collection = db.posts
businesses_collection = db["businesses"]
products_collection = db["products"] # <-- NEW
orders_collection = db["orders"]     # <-- NEW
notifications_collection = db["notifications"] # <-- Add this line
messages_collection = db["messages"] # <-- New Chat Database

# --- AUTHENTICATION ROUTES ---
@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json() or {}
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")
    
    # Extract the new location fields
    country = data.get("country", "").strip()
    state = data.get("state", "").strip()
    city = data.get("city", "").strip()

    if not name or not email or not password:
        return jsonify({"error": "Missing required core fields"}), 400
        
    if users_collection.find_one({"email": email}):
        return jsonify({"error": "Email is already registered"}), 409
        
    hashed_password = generate_password_hash(password)
    
    # Save the expanded user document
    new_user = {
        "name": name, 
        "email": email, 
        "password": hashed_password,
        "country": country,
        "state": state,
        "city": city
    }
    
    users_collection.insert_one(new_user)
    token = create_access_token(identity=email)
    return jsonify({"message": "Account created successfully", "token": token}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "All fields required"}), 400

    user = users_collection.find_one({"email": email})

    if not user or not check_password_hash(user["password"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(identity=email)

    return jsonify({
        "message": "Login successful",
        "token": token,
        "user": {
            "email": user["email"]
        }
    }), 200

# --- USER PROFILE ROUTE ---
# --- USER PROFILE ROUTE ---
@app.route("/api/profile", methods=["GET"])
@jwt_required()
def get_profile():
    user_email = get_jwt_identity()
    user = users_collection.find_one({"email": user_email})
    if not user:
        return jsonify({"error": "User not found"}), 404
        
    cursor = posts_collection.find({"author_email": user_email}).sort("created_at", -1)
    user_posts = []
    for doc in cursor:
        user_posts.append({
            "id": str(doc["_id"]),
            "content": doc["content"],
            "post_type": doc.get("post_type", "general"),
            "price": doc.get("price"),
            "created_at": doc["created_at"].strftime("%Y-%m-%d %H:%M"),
            "likes": doc.get("likes", 0),
            "comments": doc.get("comments", [])
        })
        
    # --- NEW: Fetch details of blocked users ---
    blocked_emails = user.get("blocked_users", [])
    blocked_users_list = []
    if blocked_emails:
        blocked_cursor = users_collection.find({"email": {"$in": blocked_emails}})
        for bu in blocked_cursor:
            blocked_users_list.append({
                "name": bu.get("name", "Neighbor"),
                "email": bu.get("email"),
                "avatar": bu.get("avatar", "")
            })
        
    return jsonify({
        "name": user.get("name"), 
        "email": user.get("email"), 
        "avatar": user.get("avatar", ""),
        "country": user.get("country", "Not Specified"),
        "state": user.get("state", "Not Specified"),
        "city": user.get("city", "Not Specified"),
        "posts": user_posts,
        "blocked_users": blocked_users_list # <-- Passing the data to the frontend
    }), 200

@app.route("/api/profile/avatar", methods=["PUT"])
@jwt_required()
def update_avatar():
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    avatar_data = data.get("avatar")
    
    if not avatar_data:
        return jsonify({"error": "No image data provided"}), 400
        
    # Save the Base64 image string to the user's document in MongoDB
    users_collection.update_one(
        {"email": user_email},
        {"$set": {"avatar": avatar_data}}
    )
    return jsonify({"message": "Avatar updated successfully"}), 200


@app.route("/api/profile/avatar", methods=["DELETE"])
@jwt_required()
def delete_profile_avatar():
    user_email = get_jwt_identity()
    
    try:
        # Clear out the avatar field in the database for this user
        result = users_collection.update_one(
            {"email": user_email},
            {"$set": {"avatar": ""}}
        )
        if result.matched_count == 0:
            return jsonify({"error": "User not found"}), 404
            
        return jsonify({"message": "Profile picture removed successfully"}), 200
    except Exception as e:
        return jsonify({"error": "Failed to remove profile picture"}), 500

# --- USER DISCOVERY & SOCIAL GRAPH ROUTES ---

@app.route("/api/users/search", methods=["GET"])
@jwt_required()
def search_users():
    current_user_email = get_jwt_identity()
    query = request.args.get("q", "").strip()
    
    # Get current user to check who they have blocked
    current_user = users_collection.find_one({"email": current_user_email})
    blocked_users = current_user.get("blocked_users", [])

    # Filter out the current user from the search results
    search_filter = {"email": {"$ne": current_user_email}}
    
    # NEW: Now searching across name, email, city, state, AND country
    if query:
        search_filter["$or"] = [
            {"name": {"$regex": query, "$options": "i"}},
            {"email": {"$regex": query, "$options": "i"}},
            {"city": {"$regex": query, "$options": "i"}},
            {"state": {"$regex": query, "$options": "i"}},
            {"country": {"$regex": query, "$options": "i"}}
        ]

    cursor = users_collection.find(search_filter).limit(30)
    results = []
    for u in cursor:
        is_blocked = u["email"] in blocked_users
        results.append({
            "email": u["email"],
            "name": u.get("name", "Neighbor"),
            "avatar": u.get("avatar", ""),
            "city": u.get("city", "N/A"),
            "state": u.get("state", "N/A"),
            "country": u.get("country", "N/A"),
            "is_blocked": is_blocked
        })
    return jsonify(results), 200

@app.route("/api/users/public/<email>", methods=["GET"])
@jwt_required()
def get_public_profile(email):
    current_email = get_jwt_identity()
    
    target_user = users_collection.find_one({"email": email})
    if not target_user:
        return jsonify({"error": "User not found"}), 404
        
    current_user = users_collection.find_one({"email": current_email})
    
    # Check block statuses
    blocked_by_me = email in current_user.get("blocked_users", [])
    blocked_by_them = current_email in target_user.get("blocked_users", [])

    if blocked_by_them:
        return jsonify({"error": "You do not have permission to view this profile."}), 403

    # Fetch their recent public posts
    cursor = posts_collection.find({"author_email": email}).sort("created_at", -1).limit(10)
    posts = []
    for doc in cursor:
        posts.append({
            "id": str(doc["_id"]),
            "content": doc["content"],
            "post_type": doc.get("post_type", "general"),
            "created_at": doc["created_at"].strftime("%b %d, %Y")
        })

    return jsonify({
        "name": target_user.get("name", "Neighbor"),
        "email": target_user.get("email"),
        "avatar": target_user.get("avatar", ""),
        "city": target_user.get("city", "N/A"),
        "state": target_user.get("state", "N/A"),
        "country": target_user.get("country", "N/A"),
        "is_blocked": blocked_by_me,
        "recent_posts": posts
    }), 200

@app.route("/api/users/block", methods=["POST"])
@jwt_required()
def toggle_block_user():
    current_email = get_jwt_identity()
    data = request.get_json() or {}
    target_email = data.get("email")

    if not target_email or target_email == current_email:
        return jsonify({"error": "Invalid block request"}), 400

    user = users_collection.find_one({"email": current_email})
    blocked_users = user.get("blocked_users", [])

    if target_email in blocked_users:
        # Unblock them
        users_collection.update_one({"email": current_email}, {"$pull": {"blocked_users": target_email}})
        return jsonify({"message": "User unblocked successfully", "is_blocked": False}), 200
    else:
        # Block them
        users_collection.update_one({"email": current_email}, {"$addToSet": {"blocked_users": target_email}})
        return jsonify({"message": "User blocked successfully", "is_blocked": True}), 200

# --- COMMUNITY FEED ROUTES ---
@app.route("/api/posts", methods=["POST"])
@jwt_required()
def create_post():
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    
    # Fetch author name from DB
    user = users_collection.find_one({"email": user_email})
    author_name = user.get("name", "Unknown") if user else "Unknown"

    new_post = {
        "author_email": user_email,
        "author_name": author_name,
        "content": data.get("content"),
        "post_type": data.get("post_type", "general"),
        "price": data.get("price"),
        "location": data.get("location", ""), # <-- 1. Save the new location tag
        "created_at": datetime.now(timezone.utc),
        "likes": 0,
        "comments": []
    }
    
    posts_collection.insert_one(new_post)
    return jsonify({"message": "Post created successfully"}), 201

@app.route("/api/posts", methods=["GET"])
@jwt_required()
def get_posts():
    cursor = posts_collection.find().sort("created_at", -1)
    posts = []
    for doc in cursor:
        author = users_collection.find_one({"email": doc["author_email"]})
        avatar = author.get("avatar", "") if author else ""
        
        # Format comments cleanly
        raw_comments = doc.get("comments", [])
        formatted_comments = []
        for c in raw_comments:
            c_date = c["created_at"].strftime("%b %d, %H:%M") if isinstance(c.get("created_at"), datetime) else ""
            formatted_comments.append({
                "id": c.get("id"),
                "author_name": c.get("author_name"),
                "text": c.get("text"),
                "created_at": c_date
            })
        
        posts.append({
            "id": str(doc["_id"]),
            "author_name": doc["author_name"],
            "author_email": doc["author_email"],
            "author_avatar": avatar,
            "content": doc["content"],
            "post_type": doc.get("post_type", "general"),
            "price": doc.get("price"),
            "location": doc.get("location", ""),
            "created_at": doc["created_at"].strftime("%b %d, %H:%M"),
            "liked_by": doc.get("liked_by", []), # <-- Tracks exactly who liked it
            "comments": formatted_comments
        })
    return jsonify(posts), 200

@app.route("/api/posts/<post_id>", methods=["PUT"])
@jwt_required()
def update_post(post_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    new_content = data.get("content", "").strip()
    new_location = data.get("location", "").strip()

    if not new_content:
        return jsonify({"error": "Post content cannot be empty"}), 400

    try:
        post = posts_collection.find_one({"_id": ObjectId(post_id)})
        if not post:
            return jsonify({"error": "Post not found"}), 404

        # Security Guard: Confirm ownership before updating database
        if post.get("author_email") != user_email:
            return jsonify({"error": "Unauthorized to edit this post"}), 403

        posts_collection.update_one(
            {"_id": ObjectId(post_id)},
            {"$set": {
                "content": new_content,
                "location": new_location,
                "post_type": data.get("post_type", post.get("post_type", "general")),
                "price": data.get("price", post.get("price"))
            }}
        )
        return jsonify({"message": "Post updated successfully"}), 200
    except Exception:
        return jsonify({"error": "Invalid post ID format or server error"}), 400


@app.route("/api/posts/<post_id>", methods=["DELETE"])
@jwt_required()
def delete_post(post_id):
    user_email = get_jwt_identity()

    try:
        post = posts_collection.find_one({"_id": ObjectId(post_id)})
        if not post:
            return jsonify({"error": "Post not found"}), 404

        # Security Guard: Only the author can drop this post
        if post.get("author_email") != user_email:
            return jsonify({"error": "Unauthorized to delete this post"}), 403

        posts_collection.delete_one({"_id": ObjectId(post_id)})
        return jsonify({"message": "Post deleted successfully"}), 200
    except Exception:
        return jsonify({"error": "Invalid post ID format or server error"}), 400    

# --- POST ENGAGEMENT ROUTES (LIKES & COMMENTS) ---

@app.route("/api/posts/<post_id>/like", methods=["PUT"])
@jwt_required()
def toggle_like(post_id):
    user_email = get_jwt_identity()
    try:
        post = posts_collection.find_one({"_id": ObjectId(post_id)})
        if not post: return jsonify({"error": "Post not found"}), 404

        liked_by = post.get("liked_by", [])
        
        if user_email in liked_by:
            # If already liked, clicking it again removes the like
            posts_collection.update_one({"_id": ObjectId(post_id)}, {"$pull": {"liked_by": user_email}})
        else:
            # Add the like
            posts_collection.update_one({"_id": ObjectId(post_id)}, {"$addToSet": {"liked_by": user_email}})
            
            # --- Trigger Live Notification if liking someone else's post ---
            if post["author_email"] != user_email:
                user = users_collection.find_one({"email": user_email})
                author_name = user.get("name", "A neighbor") if user else "A neighbor"
                
                notif = {
                    "recipient_email": post["author_email"],
                    "message": f"{author_name} liked your post.",
                    "type": "like",
                    "is_read": False,
                    "created_at": datetime.now(timezone.utc)
                }
                result = notifications_collection.insert_one(notif)
                notif["id"] = str(result.inserted_id)
                notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
                del notif["_id"]
                socketio.emit("new_notification", notif, to=post["author_email"])

        return jsonify({"message": "Like status updated"}), 200
    except Exception as e:
        return jsonify({"error": "Failed to process like"}), 500


@app.route("/api/posts/<post_id>/comment", methods=["POST"])
@jwt_required()
def add_comment(post_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    text = data.get("text", "").strip()

    if not text: return jsonify({"error": "Comment cannot be empty"}), 400

    try:
        post = posts_collection.find_one({"_id": ObjectId(post_id)})
        if not post: return jsonify({"error": "Post not found"}), 404

        user = users_collection.find_one({"email": user_email})
        author_name = user.get("name", "Neighbor") if user else "Neighbor"
        
        new_comment = {
            "id": str(ObjectId()),
            "author_email": user_email,
            "author_name": author_name,
            "text": text,
            "created_at": datetime.now(timezone.utc)
        }

        posts_collection.update_one({"_id": ObjectId(post_id)}, {"$push": {"comments": new_comment}})

        # --- Trigger Live Notification for new comment ---
        if post["author_email"] != user_email:
            notif = {
                "recipient_email": post["author_email"],
                "message": f"{author_name} commented on your post: '{text[:25]}...'",
                "type": "comment",
                "is_read": False,
                "created_at": datetime.now(timezone.utc)
            }
            result = notifications_collection.insert_one(notif)
            notif["id"] = str(result.inserted_id)
            notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
            del notif["_id"]
            socketio.emit("new_notification", notif, to=post["author_email"])

        return jsonify({"message": "Comment added"}), 201
    except Exception as e:
        return jsonify({"error": "Failed to post comment"}), 500    

@app.route("/api/posts/<post_id>/like", methods=["POST"])
@jwt_required()
def like_post(post_id):
    try:
        result = posts_collection.update_one({"_id": ObjectId(post_id)}, {"$inc": {"likes": 1}})
        if result.modified_count == 1:
            return jsonify({"message": "Post liked"}), 200
        return jsonify({"error": "Post not found"}), 404
    except Exception:
        return jsonify({"error": "Invalid post ID"}), 400


# --- LOCAL BUSINESS LISTINGS ROUTES ---
@app.route("/api/businesses", methods=["POST"])
@jwt_required()
def create_business():
    data = request.get_json() or {}
    name = data.get("name")
    category = data.get("category")
    address = data.get("address")
    description = data.get("description", "")
    lat = data.get("lat") 
    lng = data.get("lng")
    images = data.get("images", []) # <-- 1. Accept the unlimited images array
    
    if not name or not category or not address:
        return jsonify({"error": "Missing required business fields"}), 400
        
    user_email = get_jwt_identity()
    
    new_business = {
        "name": name,
        "category": category,
        "address": address,
        "description": description,
        "images": images, # <-- 2. Save them to the database
        "lat": lat,
        "lng": lng,
        "owner_email": user_email,
        "created_at": datetime.now(timezone.utc)
    }
    
    businesses_collection.insert_one(new_business)
    return jsonify({"message": "Business card registered successfully"}), 201

@app.route("/api/businesses", methods=["GET"])
@jwt_required()
def get_businesses():
    cursor = businesses_collection.find().sort("created_at", -1)
    businesses = []
    for doc in cursor:
        reviews = doc.get("reviews", [])
        review_count = len(reviews)
        
        # Calculate dynamic mathematical average
        avg_rating = round(sum(r["rating"] for r in reviews) / review_count, 1) if review_count > 0 else 0
        
        # Format reviews neatly for the frontend
        serialized_reviews = []
        for r in reviews:
            created_str = r["created_at"].strftime("%Y-%m-%d") if isinstance(r.get("created_at"), datetime) else ""
            serialized_reviews.append({
                "author_name": r.get("author_name", "Neighbor"),
                "author_email": r.get("author_email", ""),
                "rating": r.get("rating", 5),
                "review_text": r.get("review_text", ""),
                "created_at": created_str
            })

        businesses.append({
            "id": str(doc["_id"]),
            "name": doc["name"],
            "category": doc["category"],
            "address": doc["address"],
            "description": doc.get("description", ""),
            "images": doc.get("images", []),
            "lat": doc.get("lat"),
            "lng": doc.get("lng"),
            "owner_email": doc["owner_email"],
            "reviews": serialized_reviews,
            "average_rating": avg_rating,
            "review_count": review_count
        })
    return jsonify(businesses), 200

@app.route("/api/businesses/<business_id>", methods=["PUT"])
@jwt_required()
def update_business(business_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}

    try:
        # 1. Find the specific business in the database
        biz = businesses_collection.find_one({"_id": ObjectId(business_id)})
    except Exception:
        return jsonify({"error": "Invalid business ID format"}), 400

    if not biz:
        return jsonify({"error": "Business not found"}), 404
    
    # 2. Security Check: Ensure the person editing actually owns the listing
    if biz.get("owner_email") != user_email:
        return jsonify({"error": "Unauthorized. You do not own this listing."}), 403

    # 3. Update the data with whatever the user changed (or keep the old data if they didn't)
    update_data = {
        "name": data.get("name", biz.get("name")),
        "category": data.get("category", biz.get("category")),
        "address": data.get("address", biz.get("address")),
        "description": data.get("description", biz.get("description")),
        "images": data.get("images", biz.get("images")),
        "lat": data.get("lat", biz.get("lat")),
        "lng": data.get("lng", biz.get("lng")),
        "updated_at": datetime.now(timezone.utc)
    }

    businesses_collection.update_one({"_id": ObjectId(business_id)}, {"$set": update_data})
    return jsonify({"message": "Business updated successfully"}), 200    

@app.route("/api/businesses/<business_id>", methods=["DELETE"])
@jwt_required()
def delete_business(business_id):
    user_email = get_jwt_identity()

    try:
        biz = businesses_collection.find_one({"_id": ObjectId(business_id)})
    except Exception:
        return jsonify({"error": "Invalid business ID format"}), 400

    if not biz:
        return jsonify({"error": "Business not found"}), 404
    
    # Security Check: Ensure the person deleting actually owns the listing
    if biz.get("owner_email") != user_email:
        return jsonify({"error": "Unauthorized. You do not own this listing."}), 403

    # Delete the record permanently
    businesses_collection.delete_one({"_id": ObjectId(business_id)})
    return jsonify({"message": "Business deleted successfully"}), 200    


@app.route("/api/businesses/<business_id>/reviews", methods=["POST"])
@jwt_required()
def add_business_review(business_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    rating = data.get("rating")
    review_text = data.get("review_text", "").strip()

    if not rating or not (1 <= int(rating) <= 5):
        return jsonify({"error": "Invalid rating score. Must be between 1 and 5 stars."}), 400

    user = users_collection.find_one({"email": user_email})
    author_name = user.get("name", "Neighbor") if user else "Neighbor"

    new_review = {
        "author_email": user_email,
        "author_name": author_name,
        "rating": int(rating),
        "review_text": review_text,
        "created_at": datetime.now(timezone.utc)
    }

    try:
        businesses_collection.update_one(
            {"_id": ObjectId(business_id)},
            {"$push": {"reviews": new_review}}
        )

        # --- NEW: TRIGGER REAL-TIME NOTIFICATION ---
        biz = businesses_collection.find_one({"_id": ObjectId(business_id)})
        # Ensure we don't notify the owner if they review their own business
        if biz and biz.get("owner_email") != user_email: 
            notif = {
                "recipient_email": biz["owner_email"],
                "message": f"{author_name} left a {rating}-star review on {biz['name']}.",
                "type": "review",
                "is_read": False,
                "created_at": datetime.now(timezone.utc)
            }
            result = notifications_collection.insert_one(notif)
            
            # Format and send via WebSocket directly to the owner's screen
            notif["id"] = str(result.inserted_id)
            notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
            del notif["_id"]
            socketio.emit("new_notification", notif, to=biz["owner_email"])
        # ------------------------------------------

        return jsonify({"message": "Review added successfully"}), 201
    except Exception:
        return jsonify({"error": "Failed to log review to destination target."}), 500

@app.route("/api/messages/<other_user_email>", methods=["GET"])
@jwt_required()
def get_messages(other_user_email):
    current_user_email = get_jwt_identity()
    
    # FIXED: Corrected the variable names here so it doesn't crash!
    messages_collection.update_many(
        {"sender": other_user_email, "receiver": current_user_email, "is_read": False},
        {"$set": {"is_read": True}}
    )
    
    # Find all messages between these two specific users
    query = {
        "$or": [
            {"sender": current_user_email, "receiver": other_user_email},
            {"sender": other_user_email, "receiver": current_user_email}
        ]
    }
    cursor = messages_collection.find(query).sort("timestamp", 1)
    messages = []
    for doc in cursor:
        messages.append({
            "sender": doc["sender"],
            "receiver": doc["receiver"],
            "text": doc["text"],
            "timestamp": doc["timestamp"].strftime("%Y-%m-%d %H:%M")
        })
    return jsonify(messages), 200

# --- GET ALL ACTIVE CHATS (FOR SIDEBAR) ---
# --- GET ALL ACTIVE CHATS (FOR SIDEBAR) ---
@app.route("/api/chats", methods=["GET"])
@jwt_required()
def get_chats():
    current_user = get_jwt_identity()
    messages = list(messages_collection.find(
        {"$or": [{"sender": current_user}, {"receiver": current_user}]}
    ).sort("timestamp", -1))
    
    chat_list = []
    seen_users = set()
    
    for msg in messages:
        other_user_email = msg["receiver"] if msg["sender"] == current_user else msg["sender"]
        if other_user_email not in seen_users:
            seen_users.add(other_user_email)
            
            # Fetch the other user's profile to get their latest avatar AND NAME
            other_user = users_collection.find_one({"email": other_user_email})
            avatar = other_user.get("avatar", "") if other_user else ""
            name = other_user.get("name", "Neighbor") if other_user else "Neighbor" # <-- Fetches the Name
            
            chat_list.append({
                "email": other_user_email,
                "name": name, # <-- Sends Name to the frontend
                "last_message": msg["text"],
                "timestamp": msg["timestamp"].strftime("%b %d, %H:%M"),
                "avatar": avatar
            })
            
    return jsonify(chat_list), 200

# --- DELETE A CHAT HISTORY ---
@app.route("/api/messages/<other_user_email>", methods=["DELETE"])
@jwt_required()
def delete_chat(other_user_email):
    current_user = get_jwt_identity()
    
    # Delete all messages between the current user and this specific neighbor
    query = {
        "$or": [
            {"sender": current_user, "receiver": other_user_email},
            {"sender": other_user_email, "receiver": current_user}
        ]
    }
    messages_collection.delete_many(query)
    return jsonify({"message": "Chat deleted completely"}), 200

# WebSocket Event: User connects and joins their private "room"
@socketio.on("join")
def on_join(data):
    user_email = data.get("email")
    if user_email:
        join_room(user_email)

# WebSocket Event: Sending a live message
@socketio.on("send_message")
def handle_send_message(data):
    sender = data.get("sender")
    receiver = data.get("receiver")
    text = data.get("text")

    # 1. Save message with tracking flag
    new_message = {
        "sender": sender,
        "receiver": receiver,
        "text": text,
        "timestamp": datetime.now(timezone.utc),
        "is_read": False # <-- Initial tracking state
    }
    messages_collection.insert_one(new_message)

    # 2. Log a global notification record for the bell history
    notif = {
        "recipient_email": receiver,
        "message": f"New message from {sender}",
        "type": "chat",
        "is_read": False,
        "created_at": datetime.now(timezone.utc)
    }
    result = notifications_collection.insert_one(notif)
    
    # Format dates and IDs cleanly for WebSocket transit
    notif["id"] = str(result.inserted_id)
    notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
    del notif["_id"]

    # 3. Stream real-time events straight to the recipient's viewport
    formatted_msg = {
        "sender": sender,
        "receiver": receiver,
        "text": text,
        "timestamp": new_message["timestamp"].strftime("%b %d, %H:%M")
    }
    socketio.emit("receive_message", formatted_msg, to=receiver)
    socketio.emit("new_notification", notif, to=receiver)
    socketio.emit("unread_chat_update", to=receiver) # <-- Alerts the global navbar badge

@app.route("/api/chats/unread-count", methods=["GET"])
@jwt_required()
def get_unread_chat_count():
    user_email = get_jwt_identity()
    # Count all documents where the current user is the recipient and the message hasn't been read
    count = messages_collection.count_documents({"receiver": user_email, "is_read": False})
    return jsonify({"unread_count": count}), 200

# --- AI ASSISTANT ROUTE ---
@app.route("/api/generate-description", methods=["POST"])
@jwt_required()
def generate_description():
    data = request.get_json() or {}
    keywords = data.get("keywords", "")
    
    if not keywords:
        return jsonify({"error": "Please provide some keywords to generate a description."}), 400

    prompt = (
        f"You are an expert copywriter for a premium neighborhood directory. "
        f"Write a short, engaging, and highly professional description (maximum 3 sentences) "
        f"for a local business listing based on these raw keywords: {keywords}. "
        f"Do not use hashtags or emojis."
    )

    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)
        return jsonify({"generated_text": response.text.strip()}), 200
    except Exception as e:
        # 1. Print the real error to your VS Code terminal
        print(f"CRITICAL GEMINI ERROR: {str(e)}") 
        # 2. Send the real error directly to your browser alert
        return jsonify({"error": f"AI Error: {str(e)}"}), 500

# --- REAL-TIME NOTIFICATION ROUTES ---
@app.route("/api/notifications", methods=["GET"])
@jwt_required()
def get_notifications():
    user_email = get_jwt_identity()
    cursor = notifications_collection.find({"recipient_email": user_email}).sort("created_at", -1).limit(20)
    notifs = []
    for doc in cursor:
        notifs.append({
            "id": str(doc["_id"]),
            "message": doc["message"],
            "type": doc["type"],
            "is_read": doc.get("is_read", False),
            "created_at": doc["created_at"].strftime("%b %d, %H:%M")
        })
    return jsonify(notifs), 200

@app.route("/api/notifications/read", methods=["PUT"])
@jwt_required()
def mark_notifications_read():
    user_email = get_jwt_identity()
    notifications_collection.update_many(
        {"recipient_email": user_email, "is_read": False},
        {"$set": {"is_read": True}}
    )
    return jsonify({"message": "Notifications marked as read"}), 200

# ==========================================
# --- HYPERLOCAL MARKETPLACE ENGINE ---
# ==========================================

@app.route("/api/products", methods=["GET"])
@jwt_required()
def get_products():
    cursor = products_collection.find().sort("created_at", -1)
    products = []
    for doc in cursor:
        products.append({
            "id": str(doc["_id"]),
            "title": doc["title"],
            "description": doc.get("description", ""),
            "price": doc["price"],
            "category": doc.get("category", "General"),
            "images": doc.get("images", []),
            "seller_name": doc["seller_name"],
            "seller_email": doc["seller_email"],
            "created_at": doc["created_at"].strftime("%b %d, %Y")
        })
    return jsonify(products), 200

@app.route("/api/products", methods=["POST"])
@jwt_required()
def create_product():
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    
    user = users_collection.find_one({"email": user_email})
    seller_name = user.get("name", "Neighbor") if user else "Neighbor"

    new_product = {
        "title": data.get("title"),
        "description": data.get("description", ""),
        "price": float(data.get("price", 0)),
        "category": data.get("category", "General"),
        "images": data.get("images", []),
        "seller_email": user_email,
        "seller_name": seller_name,
        "created_at": datetime.now(timezone.utc)
    }
    
    products_collection.insert_one(new_product)
    return jsonify({"message": "Product listed successfully!"}), 201

@app.route("/api/checkout", methods=["POST"])
@jwt_required()
def process_checkout():
    buyer_email = get_jwt_identity()
    data = request.get_json() or {}
    cart = data.get("cart", [])
    total = data.get("total", 0)

    if not cart:
        return jsonify({"error": "Cart is empty"}), 400

    buyer = users_collection.find_one({"email": buyer_email})
    buyer_name = buyer.get("name", "A neighbor") if buyer else "A neighbor"

    # 1. Create the Order Record
    new_order = {
        "buyer_email": buyer_email,
        "buyer_name": buyer_name,
        "items": cart,
        "total": total,
        "status": "Pending Confirmation", # Statuses: Pending, Preparing, Ready for Pickup
        "created_at": datetime.now(timezone.utc)
    }
    orders_collection.insert_one(new_order)

    # 2. Notify all unique sellers involved in this order via WebSockets
    notified_sellers = set()
    for item in cart:
        seller_email = item.get("seller_email")
        if seller_email and seller_email not in notified_sellers and seller_email != buyer_email:
            notified_sellers.add(seller_email)
            
            notif = {
                "recipient_email": seller_email,
                "message": f"🎉 {buyer_name} just placed an order for your items!",
                "type": "order",
                "is_read": False,
                "created_at": datetime.now(timezone.utc)
            }
            result = notifications_collection.insert_one(notif)
            notif["id"] = str(result.inserted_id)
            notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
            del notif["_id"]
            
            socketio.emit("new_notification", notif, to=seller_email)

    return jsonify({"message": "Order placed successfully!"}), 200

@app.route("/api/products/<product_id>", methods=["PUT"])
@jwt_required()
def update_product(product_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}

    try:
        product = products_collection.find_one({"_id": ObjectId(product_id)})
    except Exception:
        return jsonify({"error": "Invalid product ID format"}), 400

    if not product:
        return jsonify({"error": "Product not found"}), 404
    
    # Security Check: Ensure the user editing actually owns the item listing
    if product.get("seller_email") != user_email:
        return jsonify({"error": "Unauthorized. You do not own this listing."}), 403

    # Update product parameters cleanly
    update_data = {
        "title": data.get("title", product.get("title")),
        "category": data.get("category", product.get("category")),
        "price": float(data.get("price", product.get("price"))),
        "description": data.get("description", product.get("description")),
        "images": data.get("images", product.get("images")),
        "updated_at": datetime.now(timezone.utc)
    }

    products_collection.update_one({"_id": ObjectId(product_id)}, {"$set": update_data})
    return jsonify({"message": "Product listing updated successfully"}), 200

@app.route("/api/products/<product_id>", methods=["DELETE"])
@jwt_required()
def delete_product(product_id):
    user_email = get_jwt_identity()

    try:
        product = products_collection.find_one({"_id": ObjectId(product_id)})
    except Exception:
        return jsonify({"error": "Invalid product ID format"}), 400

    if not product:
        return jsonify({"error": "Product not found"}), 404
    
    # Security Check: Only the seller can delete their item
    if product.get("seller_email") != user_email:
        return jsonify({"error": "Unauthorized. You do not own this listing."}), 403

    products_collection.delete_one({"_id": ObjectId(product_id)})
    return jsonify({"message": "Product deleted successfully"}), 200

# --- MARKETPLACE FULFILLMENT & ORDER MANAGEMENT ---

@app.route("/api/orders/seller", methods=["GET"])
@jwt_required()
def get_seller_orders():
    user_email = get_jwt_identity()
    
    # Fetch all orders where at least one item belongs to this seller
    cursor = orders_collection.find({"items.seller_email": user_email}).sort("created_at", -1)
    orders = []
    for doc in cursor:
        # Filter item array to display only what belongs to this specific seller
        my_items = [item for item in doc["items"] if item.get("seller_email") == user_email]
        
        orders.append({
            "id": str(doc["_id"]),
            "buyer_name": doc.get("buyer_name", "Neighbor"),
            "buyer_email": doc.get("buyer_email"),
            "items": my_items,
            "total": doc.get("total"),
            "payment_method": doc.get("payment_method", "Cash on Delivery"),
            "delivery_address": doc.get("delivery_address", ""),
            "status": doc.get("status", "Pending Confirmation"),
            "created_at": doc["created_at"].strftime("%b %d, %H:%M")
        })
    return jsonify(orders), 200


@app.route("/api/orders/buyer", methods=["GET"])
@jwt_required()
def get_buyer_orders():
    user_email = get_jwt_identity()
    
    # Fetch all orders placed by this current logged in account
    cursor = orders_collection.find({"buyer_email": user_email}).sort("created_at", -1)
    orders = []
    for doc in cursor:
        orders.append({
            "id": str(doc["_id"]),
            "items": doc.get("items", []),
            "total": doc.get("total"),
            "payment_method": doc.get("payment_method"),
            "delivery_address": doc.get("delivery_address"),
            "status": doc.get("status", "Pending Confirmation"),
            "created_at": doc["created_at"].strftime("%b %d, %H:%M")
        })
    return jsonify(orders), 200


@app.route("/api/orders/<order_id>/status", methods=["PUT"])
@jwt_required()
def update_order_status(order_id):
    user_email = get_jwt_identity()
    data = request.get_json() or {}
    new_status = data.get("status")

    valid_statuses = ["Pending Confirmation", "Preparing", "Ready for Pickup", "Out for Neighborhood Delivery", "Completed"]
    if new_status not in valid_statuses:
        return jsonify({"error": "Invalid status value allocation"}), 400

    try:
        order = orders_collection.find_one({"_id": ObjectId(order_id)})
        if not order:
            return jsonify({"error": "Order item payload missing"}), 404

        # Security check: Ensure this user is the seller for at least one item in the order
        is_seller = any(item.get("seller_email") == user_email for item in order.get("items", []))
        if not is_seller:
            return jsonify({"error": "Unauthorized. You are not the seller of this order."}), 403

        # Update order status parameters
        orders_collection.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": new_status}})

        # Trigger real-time alert back to the buyer via WebSockets
        buyer_email = order.get("buyer_email")
        if buyer_email:
            notif = {
                "recipient_email": buyer_email,
                "message": f"📦 Your order status has been updated to: {new_status}!",
                "type": "order_update",
                "is_read": False,
                "created_at": datetime.now(timezone.utc)
            }
            result = notifications_collection.insert_one(notif)
            notif["id"] = str(result.inserted_id)
            notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
            del notif["_id"]
            
            socketio.emit("new_notification", notif, to=buyer_email)

        return jsonify({"message": "Order tracker updated successfully"}), 200
    except Exception:
        return jsonify({"error": "Invalid payload format processing"}), 500   

@app.route("/api/orders/<order_id>/cancel", methods=["PUT"])
@jwt_required()
def cancel_order(order_id):
    user_email = get_jwt_identity()
    
    try:
        order = orders_collection.find_one({"_id": ObjectId(order_id)})
        if not order:
            return jsonify({"error": "Order transaction log missing"}), 404

        if order.get("status") == "Completed":
            return jsonify({"error": "Cannot cancel an order that has already been completed and received."}), 400

        is_buyer = order.get("buyer_email") == user_email
        is_seller = any(item.get("seller_email") == user_email for item in order.get("items", []))

        if not (is_buyer or is_seller):
            return jsonify({"error": "Unauthorized assignment parameters"}), 403

        # Assign cancellation signature tags based on actor type
        cancellation_status = "Cancelled by Buyer" if is_buyer else "Cancelled by Seller"
        orders_collection.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": cancellation_status}})

        # Cross-notify the opposing party over system channels
        recipient = order.get("seller_email") if is_buyer else order.get("buyer_email")
        actor_title = "The buyer" if is_buyer else "The seller"
        
        if recipient:
            notif = {
                "recipient_email": recipient,
                "message": f"🚨 {actor_title} has cancelled order reference #{order_id[-6:]}.",
                "type": "order_cancelled",
                "is_read": False,
                "created_at": datetime.now(timezone.utc)
            }
            result = notifications_collection.insert_one(notif)
            notif["id"] = str(result.inserted_id)
            notif["created_at"] = notif["created_at"].strftime("%b %d, %H:%M")
            del notif["_id"]
            
            socketio.emit("new_notification", notif, to=recipient)

        return jsonify({"message": f"Order successfully marked as {cancellation_status}"}), 200
    except Exception:
        return jsonify({"error": "Failed to process cancellation request"}), 500         
       

# --- GLOBAL AI CHATBOT ROUTE ---
@app.route("/api/chatbot", methods=["POST"])
@jwt_required(optional=True) # Allows users to chat even if not logged in
def ai_chatbot():
    data = request.get_json() or {}
    user_message = data.get("message")
    # History array allows the AI to remember the conversation context
    frontend_history = data.get("history", []) 

    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    try:
        # Dynamically find the best available model (reusing your bulletproof logic)
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        chosen_model = valid_models[0] if valid_models else 'gemini-2.5-flash'
        for m in valid_models:
            if "flash" in m or "pro" in m:
                chosen_model = m
                break

        # Define the AI's persona and rules
        system_instruction = (
            "You are the friendly and expert AI guide for a hyperlocal neighborhood platform called YourNeighborhood. "
            "Your job is to help users navigate the platform, give them roadmaps for local businesses, "
            "and answer community questions. Keep your answers concise, conversational, and helpful. "
            "Format your responses cleanly."
        )

        # Initialize the model with the persona
        model = genai.GenerativeModel(
            model_name=chosen_model,
            system_instruction=system_instruction
        )

        # Start a chat session using the history passed from the frontend
        chat = model.start_chat(history=frontend_history)
        
        # Send the new message
        response = chat.send_message(user_message)

        return jsonify({
            "reply": response.text
        }), 200

    except Exception as e:
        print(f"CHATBOT ERROR: {str(e)}")
        return jsonify({"error": "I am experiencing network interference. Please try again in a moment."}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # host='0.0.0.0' is required for cloud hosting
    socketio.run(app, host='0.0.0.0', port=port)
