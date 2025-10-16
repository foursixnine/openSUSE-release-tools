import os
import requests
from urllib.parse import urljoin
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(levelname)-2s %(name)-2s %(funcName)s:%(lineno)d: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


class Gitea:
    """Gitea API interface"""

    @staticmethod
    def get_token():
        ret = os.environ.get("GITEA_ACCESS_TOKEN")
        if ret is None:
            raise RuntimeError("Environment variable GITEA_ACCESS_TOKEN is not set")
        return ret

    def __init__(self, url):
        self.base_url = urljoin(url, "/api/v1/")
        self.token = self.get_token()
        self.logger = logging.getLogger(__name__)
        self.logger.debug(f"Initialized correctly, api base is: {self.base_url}")

    def request(self, method, path, **kwargs):
        arg_headers = kwargs.get('headers') or {}
        headers = {'Authorization': f'token {self.token}'}
        headers.update(arg_headers)
        kwargs['headers'] = headers

        url = urljoin(self.base_url, path)
        self.logger.debug(f"Querying: {method} {url}")
        return requests.request(method, url, **kwargs)

    def get(self, path, **kwargs):
        self.logger.debug(f"GET: {path}")
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        self.logger.debug(f"POST: {path}")
        return self.request('POST', path, **kwargs)

    def delete(self, path, **kwargs):
        self.logger.debug(f"DELETE: {path}")
        return self.request('DELETE', path, **kwargs)

    def get_open_prs_for_project_branch(self, project, branch, build_bot_username):
        open_requests = []
        requests = self.get(f"repos/{project}/pulls?state=open&base_branch={branch}")

        if requests.status_code == 404:
            self.logger.error(f"'{project}' does not exist")
            return open_requests

        pull_requests = requests.json()
        if not pull_requests:
            self.logger.warning(f"No pull requests found for '{project}'")
            return open_requests

        for req in pull_requests:
            request = Request(req, build_bot_username)
            if request.target_branch == branch:
                open_requests.append(request)
                self.logger.debug(f"Got a PR: {request}")
            else:
                self.logger.debug(f"discarding {request} because '{request.target_branch}' is not the requested branch '{branch}'")

        return open_requests


class Request:
    def __init__(self, req, build_bot_username):
        self.pr_url = req["url"]
        self.target_branch = req["base"]["label"]
        self.owner = req["base"]["repo"]["owner"]["login"]
        self.repo = req["base"]["repo"]["name"]
        self.author = req["user"]["login"]
        self.title = req["title"]
        self.id = req["number"]
        self.build_status = None
        self.reviews = []
        self.review_events = {}
        self.build_bot_username = build_bot_username

    def __str__(self):
        return f"{self.owner}/{self.repo} PR#{self.id} by {self.author}"

    def is_build_finished(self):
        for review in self.reviews:
            if (review.by == self.build_bot_username and review.state == "APPROVED" and
                    self.review_events[review.by]["created_at"] == review.submitted_at and
                    not review.dismissed):
                logger.info(f"*** Build is finished - {review} -> {self}")
                return True
            elif review.by == self.build_bot_username and review.state != "APPROVED":
                logger.info(f"+++ Build is not finished {review.state} <- {self} ")
                if self.review_events[review.by]["created_at"] != review.submitted_at:
                    logger.debug(f"### {self} {review} timestamps don't match with timeline")
                return False
            else:
                logger.debug(f"--- Discarding review {review} dismissed? ({review.dismissed}) for {self} ")
                continue

    def get_requested_reviews(self, gitea):
        review = gitea.get(f"repos/{self.owner}/{self.repo}/pulls/{self.id}/reviews")
        for review in review.json():
            logger.debug(f"Requested reviews: PR#{self.id} got request for {review["user"]["login"]} with state {review["state"]}")
            self.reviews.append(Review(review))
            logger.debug(f"Review appended: PR#{self.id} - {review["user"]["login"]} with state {review["state"]}")

    def get_reviews_by_timeline(self, gitea):
        request = gitea.get(f"repos/{self.owner}/{self.repo}/issues/{self.id}/timeline")

        if request.status_code == 404:
            self.logger.error(f"'{self}' does not have a timeline")
            # this should throw an exception
            return

        timeline = request.json()
        timeline.reverse()

        self.review_events = {}
        # reset the timeline every time a pull_push event happens
        for event in timeline:
            if event["type"] == "pull_push":
                logger.debug(f"*** All events since last push ({event["body"]}) have been processed for {self}")
                breakpoint()
                break

            elif event["type"] == "review":
                logger.debug(f"--- Review found in timeline: {event["type"]} - {event["id"]}")
                # Only take the latest review for a given user
                if event["user"]["login"] not in self.review_events:
                    self.review_events[event["user"]["login"]] = event
                else:
                    logger.debug(f"Got multiple reviews for {event["user"]["login"]}")

            else:
                logger.debug(f"Discarding event type: {event["type"]} - {event["id"]}")
                continue

        self.get_requested_reviews(gitea)


class Review:
    def __init__(self, review):
        self.by = review["user"]["login"]
        self.state = review["state"]
        self.submitted_at = review["submitted_at"]
        self.stale = review["stale"]
        self.dismissed = review["dismissed"]

    def __str__(self):
        return f"{self.by} with state {self.state}"
