#!/usr/bin/env python3

# PRBot - monitor a GitHub repository for new pull requests and post a comment
# Copyright (C) 2016-2017 Andrew Donnellan <andrew@donnellan.id.au>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import traceback
import os
import json
import github
import jinja2
import time
import jwt
import requests
import calendar

# Read all settings from environment variables
GITHUB_APP_CLIENT_ID = os.environ.get('GITHUB_APP_CLIENT_ID')
GITHUB_APP_PRIVATE_KEY_PATH = os.environ.get('GITHUB_APP_PRIVATE_KEY_PATH')
MESSAGE_PATH = os.environ.get('MESSAGE_PATH')
STATUS_FILE = os.environ.get('STATUS_FILE')
REPO_NAME = os.environ.get('REPO_NAME')
AUTO_CLOSE = os.environ.get('AUTO_CLOSE', 'false').lower() == 'true'

# Validate required environment variables
required_vars = ['GITHUB_APP_CLIENT_ID', 'GITHUB_APP_PRIVATE_KEY_PATH', 'MESSAGE_PATH', 'STATUS_FILE', 'REPO_NAME']
for var in required_vars:
    if not os.environ.get(var):
        raise ValueError(f"Required environment variable {var} is not set")

def get_installation_token(client_id, private_key_path, repo_name):
    """Generate a JWT and get an installation token for the repository."""
    # Extract organization from repo name (format: org/repo)
    org_name = repo_name.split('/')[0]

    # Read private key
    with open(private_key_path, 'r') as f:
        private_key = f.read()

    # Generate JWT
    now = int(time.time())
    payload = {
        'iat': now - 60,  # 60 seconds in the past
        'exp': now + (10 * 60),  # 10 minutes maximum
        'iss': client_id
    }
    jwt_token = jwt.encode(payload, private_key, algorithm='RS256')

    # Get installation ID for the organization
    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {jwt_token}',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    # Try to get organization installation
    response = requests.get(
        f'https://api.github.com/orgs/{org_name}/installation',
        headers=headers
    )

    if response.status_code != 200:
        raise Exception(f"Failed to get installation ID: {response.status_code} - {response.text}")

    installation_id = response.json()['id']

    # Generate installation token
    response = requests.post(
        f'https://api.github.com/app/installations/{installation_id}/access_tokens',
        headers=headers
    )

    if response.status_code != 201:
        raise Exception(f"Failed to get installation token: {response.status_code} - {response.text}")

    token_data = response.json()
    return token_data['token'], token_data['expires_at']

def get_or_refresh_token(status, client_id, private_key_path, repo_name):
    """Get a valid installation token, refreshing if expired."""
    current_time = time.time()

    # Check if we have a valid token with 5-minute buffer
    if ('installation_token' in status and
        'token_expires_at' in status and
        status['installation_token'] and
        status['token_expires_at']):
        try:
            # Parse expiration time from ISO format
            expires_at_str = status['token_expires_at']
            expires_at_struct = time.strptime(expires_at_str, "%Y-%m-%dT%H:%M:%SZ")
            expires_at_timestamp = calendar.timegm(expires_at_struct)

            # If token expires in more than 5 minutes, use it
            if expires_at_timestamp > current_time + 300:
                return status['installation_token']
        except (ValueError, KeyError):
            # If parsing fails, fall through to refresh
            pass

    # Get new token
    token, expires_at = get_installation_token(client_id, private_key_path, repo_name)

    # Update status
    status['installation_token'] = token
    status['token_expires_at'] = expires_at

    return token

def poll(repo, msg, status):
    """Monitor pull requests and post comments."""
    pulls = repo.get_pulls(sort='created')
    threshold = status['pull_req_number']
    for pull in pulls.reversed:
        print("Pull request #{} by @{}: {}".format(pull.number, pull.user.login, pull.title))
        if pull.number <= threshold:
            print(" => Lower than threshold ({}), breaking".format(threshold))
            break
        comment = msg.render(username=pull.user.login)
        try:
            if pull.closed_at:
                print(" => Pull request closed. Skipping...")
                continue

            # Double check that we haven't posted on this before...
            # Since we can't get the app username via /user, we'll check all comments
            # and skip if ANY comment contains our template text
            existing_comments = pull.get_issue_comments()

            # Check if any comment contains the same template-generated text
            # This is a simple heuristic to avoid duplicates
            comment_text = comment.strip()
            if any(comment_text in c.body for c in existing_comments):
                print(" => Similar comment detected. Skipping...")
                continue

            pull.create_issue_comment(comment)
            if AUTO_CLOSE:
                pull.edit(state="closed")
                print(" => Comment posted and Pull Request closed successfully")
            else:
                print(" => Comment posted successfully")
            status['pull_req_number'] = max(status['pull_req_number'], pull.number)
        except:
            print(" => Error occurred when posting comment")
            print("\n".join([" =>  " + line for line in traceback.format_exc().splitlines()]))

def main():
    try:
        status = json.load(open(STATUS_FILE))
    except FileNotFoundError:
        status = {}

    # Ensure status has all required fields with defaults
    status.setdefault('pull_req_number', 0)
    status.setdefault('installation_token', None)
    status.setdefault('token_expires_at', None)

    # Get or refresh installation token
    token = get_or_refresh_token(
        status,
        GITHUB_APP_CLIENT_ID,
        GITHUB_APP_PRIVATE_KEY_PATH,
        REPO_NAME
    )

    msg = jinja2.Template(open(MESSAGE_PATH).read())
    gh = github.MainClass.Github(token)
    repo = gh.get_repo(REPO_NAME)

    print("Bot is posting as GitHub App installation")

    poll(repo, msg, status)
    json.dump(status, open(STATUS_FILE, 'w'))

if __name__ == '__main__':
    main()