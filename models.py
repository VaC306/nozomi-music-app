from __future__ import annotations

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class SpotifyUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    spotify_user_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=False, default="Spotify User")
    email = db.Column(db.String(255), nullable=False, default="")
    profile_url = db.Column(db.Text, nullable=False, default="")
    image_url = db.Column(db.Text, nullable=False, default="")
    access_token = db.Column(db.Text, nullable=False, default="")
    refresh_token = db.Column(db.Text, nullable=False, default="")
    token_expires_at = db.Column(db.Integer, nullable=False, default=0)
    client_id = db.Column(db.Text, nullable=False, default="")
    client_secret = db.Column(db.Text, nullable=False, default="")
    redirect_uri = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
