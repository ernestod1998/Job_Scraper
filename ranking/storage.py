"""Public, durable checkpoints on a dedicated branch, using optimistic writes."""
import base64
import json
import urllib.error
import urllib.request
from urllib.parse import quote


class GitHubStore:
    def __init__(self, repo, token):
        self.origin = 'https://api.github.com/repos/' + repo
        self.token = token
        self.branch = 'ranking-state'

    def request(self, path, data=None, method=None):
        req = urllib.request.Request(self.origin + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError('checkpoint_http_' + str(exc.code)) from None

    def initialize(self):
        if self.request('/git/ref/heads/' + self.branch) is None:
            main = self.request('/git/ref/heads/main')
            self.request('/git/refs', {'ref': 'refs/heads/' + self.branch, 'sha': main['object']['sha']})

    def get(self, path):
        item = self.request('/contents/' + quote(path) + '?ref=' + self.branch)
        return (json.loads(base64.b64decode(item['content'])), item['sha']) if item else (None, None)

    def put(self, path, value, sha=None):
        body = {'branch': self.branch, 'message': 'chore: daily ranking checkpoint',
                'content': base64.b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()}
        if sha:
            body['sha'] = sha
        result = self.request('/contents/' + quote(path), body, 'PUT')
        return result['content']['sha']
