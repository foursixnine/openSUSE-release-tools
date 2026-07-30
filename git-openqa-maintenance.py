#!/usr/bin/env python3
import sys
import os
import re
import argparse
import requests
import logging
from openqa_client.client import OpenQA_Client
from urllib.parse import urlencode, urlunparse, urlparse
from lxml import etree as ET
from collections import namedtuple
import osc.core

dry_run = True
openqa_dry_run = True
USER_AGENT = (
    "git-openqa-maintenance (https://github.com/openSUSE/openSUSE-release-tools)"
)

log = logging.getLogger(sys.argv[0] if __name__ == "__main__" else __name__)
log.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(name)-2s %(levelname)-2s %(funcName)s:%(lineno)d: %(message)s"
)
handler.setFormatter(formatter)
log.addHandler(handler)

CONFIG_DATA = {
    "products/PackageHub": {
        "repo_template": "openSUSE:Backports:SLE-{version}:PullRequest:{pr_id}",
        "OS_TEST_TEMPLATE": "openSUSE:/Backports:/SLE-{version}:/PullRequest:/@INCIDENTNR@/standard",
    },
    "openSUSE/Leap": {
        "repo_template": "openSUSE:Leap:{version}:PullRequest:{pr_id}",
        "OS_TEST_TEMPLATE": "openSUSE:/Leap:/{version}:/PullRequest:/@INCIDENTNR@/standard",
    },
    "openSUSE/LeapNonFree": {
        "repo_template": "openSUSE:Leap:{version}:NonFree:PullRequest:{pr_id}",
        "OS_TEST_TEMPLATE": "openSUSE:/Leap:/{version}:/NonFree:/PullRequest:/@INCIDENTNR@/standard",
    },
}

GITEA_HOST = None
GITEA_TOKEN = None
BS_HOST = None
REPO_PREFIX = None
REVIEW_GROUP = None
openqa = None
OPENQA_FORCE_NEW_BUILD = ""

# Variables to know status of QA
QA_UNKNOWN = 0
QA_INPROGRESS = 1
QA_FAILED = 2
QA_PASSED = 3


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--myself", help="Username of bot", default="openqa-maintenance"
    )
    parser.add_argument(
        "--review-group",
        help="Group to be used for approval",
        default="@qam-openqa-review",
    )
    parser.add_argument(
        "--openqa-host", help="OpenQA instance url", default="http://localhost:9526"
    )
    parser.add_argument(
        "--verbose", help="Verbosity", default="1", type=int, choices=[0, 1, 2, 3]
    )
    parser.add_argument("--branch", help="Target branch, eg. leap-16.0")
    parser.add_argument("--project", help="Target project")
    parser.add_argument("--pr-id", help="PR to trigger tests for")
    parser.add_argument(
        "--gitea", help="Gitea instance to use", default="https://src.opensuse.org"
    )
    parser.add_argument(
        "--bs", help="Build service api", default="https://api.opensuse.org"
    )
    parser.add_argument(
        "--bs-bot", help="Build service bot", default="autogits_obs_staging_bot"
    )
    parser.add_argument(
        "--repo-prefix",
        help="Build service repository",
        default="http://download.opensuse.org/repositories",
    )

    args = parser.parse_args()
    return args


def process_project(args):
    pull_requests = get_open_prs_for_project_branch(args.project, args.branch)
    for req in pull_requests:
        process_pull_request(req, args)

    log.info("Finished, processed %d pull requests", len(pull_requests))


def get_open_prs_for_project_branch(project, branch):
    limit = 50
    page = 1
    pull_requests = []

    while True:
        pull_requests_url = (
            GITEA_HOST
            + f"/api/v1/repos/{project}/pulls?state=open&base_branch={branch}&limit={limit}&page={page}"
        )

        try:
            request = request_get(pull_requests_url)
        except requests.exceptions.HTTPError as e:
            log.error(f"Project '{project}' doesn't exist: {e}")
            return []

        if not request:
            break

        pull_requests.extend(request)

        if len(request) < limit:
            break

        page += 1

    if not pull_requests:
        log.warning(f"No pull requests found for '{project}' on'{branch}'")
        return []

    pr_numbers = [req["number"] for req in pull_requests]
    num_prs = len(pr_numbers)
    log.debug(f"Found {num_prs} pull requests for '{project}' on'{branch}'")
    return pr_numbers


def process_pull_request(pr_id, args):
    data = gitea_query_pr(args.project, pr_id)

    pr = data["number"]
    project = data["base"]["repo"]["full_name"]
    branch = data["base"]["label"]
    log.info(f"working on {project}#{pr}")

    if branch != args.branch or project != args.project:
        log.error(f"PR {project}#{pr} does not match target {args.branch}, skipping")
        return

    build_review_id, openqa_build_overview, previous_review, ignore_last_review, force_new_build = get_events_by_timeline(project, pr, args.bs_bot)
    if not is_build_finished(project, pr, build_review_id):
        log.info(
            f"Build for {project}#{pr} is not ready, not needed or is broken. Skipping."
        )
        return

    obs_project, bs_repo_url, os_test_template = get_obs_values(project, branch, pr)

    if not obs_project:
        log.error(f"Could not compute branch version for {project}#{pr}.")
        return

    # We need to query every package in the staged update
    packages_in_project = get_packages_from_obs_project(obs_project)

    if not packages_in_project:
        log.warning(f"No packages found in {obs_project}, skipping.")
        return

    settings = prepare_update_settings(
        project, obs_project, os_test_template, bs_repo_url, pr, packages_in_project
    )
    openqa_job_params = prepare_openqa_job_params(args, obs_project, data, settings)

    openqa_trigger_reason = f"Build for {project}#{pr} has no openQA tests yet"
    openqa_trigger_tests = False

    # if there's a comment by us, tests have been triggered, so lets check the status
    if openqa_build_overview and not force_new_build:
        log.info(f"Build for {project}#{pr} has openQA tests")
        log.debug(f"openQA tests are at {openqa_build_overview}")

        if not previous_review:
            qa_state = compute_openqa_tests_status(openqa_job_params)
            take_action(project, pr, qa_state, openqa_build_overview)
        elif ignore_last_review:
            log.info(
                f"Build for {project}#{pr} has a review by us, but it has been dismissed. Checking tests again"
            )

            openqa_trigger_reason = "Not needed"
            openqa_restart_failed_tests(openqa_job_params)

        else:
            log.info(
                f"Build for {project}#{pr} has a review already by us: {previous_review}"
            )

    else:
        openqa_trigger_tests = True
        openqa_trigger_reason = "A new openQA build is being requested" if force_new_build else openqa_trigger_reason

    if openqa_trigger_tests:
        log.info(f"Triggering openQA tests for {project}#{pr} - Reason: {openqa_trigger_reason}")
        trigger_openqa_build(args, pr, project, openqa_job_params)

def trigger_openqa_build(args, pr, project, openqa_job_params):
    openqa_build_overview = openqa_schedule(args, openqa_job_params)
        # instead of using the statuses api, we will have to use the comments api
        # to report that tests have been triggered, and approve
        # gitea_post_status(openqa_job_params["GITEA_STATUSES_URL"], openqa_build_overview)
    gitea_post_build_overview(project, pr, openqa_build_overview)
    log.info(f"Build triggered, results at {openqa_build_overview}")


def take_action(project, pr, qa_state, openqa_build_overview):
    if qa_state == QA_UNKNOWN:
        log.error(f"QA state is QA_UNKNOWN for {project}#{pr}")

    elif qa_state == QA_INPROGRESS:
        log.info(f"Tests are still running for {project}#{pr}, not taking any action")

    elif qa_state == QA_FAILED or qa_state == QA_PASSED:
        if qa_state == QA_PASSED:
            msg = f"openQA tests passed: {openqa_build_overview}\n"
            msg += f"{REVIEW_GROUP}: approve"

        else:
            msg = (
                f"openQA tests failed: {openqa_build_overview}\n"
                + f"{REVIEW_GROUP}: decline\n"
                + f"\nDismiss the review for {REVIEW_GROUP} to ignore this review\n"
                + f"The last comment that only mentions {MYSELF} will force a fresh"
                + f"openQA build if {REVIEW_GROUP} review is till pending."
            )

        gitea_post_openqa_review(project, pr, msg)


def compute_openqa_tests_status(openqa_job_params):
    values = {
        "distri": openqa_job_params["DISTRI"],
        "version": openqa_job_params["VERSION"],
        "arch": openqa_job_params["ARCH"],
        "flavor": openqa_job_params["FLAVOR"],
        "build": openqa_job_params["BUILD"],
        "scope": "relevant",
        "latest": "1",
    }
    jobs = openqa.openqa_request("GET", "jobs", values)["jobs"]
    # this comes from openqabot.py#calculate_qa_status
    if not jobs:
        return QA_UNKNOWN

    j = {}
    has_failed = False
    in_progress = False

    for job in jobs:
        if job["clone_id"]:
            continue
        name = job["name"]

        if name in j and int(job["id"]) < int(j[name]["id"]):
            continue
        j[name] = job

        if job["state"] not in ("cancelled", "done"):
            in_progress = True
        else:
            if job["result"] != "passed" and job["result"] != "softfailed":
                has_failed = True

    if not j:
        return QA_UNKNOWN
    if in_progress:
        return QA_INPROGRESS
    if has_failed:
        return QA_FAILED

    return QA_PASSED

def openqa_restart_failed_tests(openqa_job_params):
    values = {
        "distri": openqa_job_params["DISTRI"],
        "version": openqa_job_params["VERSION"],
        "arch": openqa_job_params["ARCH"],
        "flavor": openqa_job_params["FLAVOR"],
        "build": openqa_job_params["BUILD"],
        # result__not=none&result__not=passed&result__not=softfailed
        "result__not": "none",
        "result__not": "passed",
        "result__not": "softfailed",
        "scope": "relevant",
        "latest": "1",
    }
    jobs = openqa.openqa_request("GET", "jobs", values)["jobs"]
    for job in jobs:
        log.info(f"Restarting failed test {job['id']} ({job['name']})")
        openqa.openqa_request("POST", f"jobs/{job['id']}/restart")


def is_build_finished(project, pr, review_id):
    if not review_id:
        log.warning(
            f"Could not find build review_id for {project}#{pr}. Assuming build is not finished."
        )
        return False

    review = get_build_review_status(project, pr, review_id)
    if review["state"] == "APPROVED":
        if review["body"] == "Build successful":
            log.info(f"Build is finished for {project}#{pr}")
            return True
        elif (
            review["body"]
            == "No package changes, not rebuilding project by default, accepting change"
        ):
            log.info(f"No build has been triggered for {project}#{pr}")
            return False
        else:
            log.error(f"Unknown build state for {project}#{pr}: {review['body']}")
            return False
    else:
        log.warning(f"Build is in state {review['state']} for {project}#{pr}")
        return False


def get_build_review_status(project, pr, review_id):
    return gitea_get_review(project, pr, review_id)


def prepare_update_settings(
    project, obs_project, os_test_template, bs_repo_url, pr, packages
):
    settings = {}
    staged_update_name = get_staged_update_name(obs_project)
    build_project = project.replace("/", "_")
    # this could also be: obs_project.split(':')[-1]
    # start with a colon so it looks cool behind 'Build' :/
    settings["BUILD"] = f":{build_project}:{pr}:{staged_update_name}"
    settings["INCIDENT_REPO"] = bs_repo_url
    # so tests can do zypper in -t patch $INCIDENT_PATCH
    patch_id = obs_project.replace(":", "_")
    settings["INCIDENT_PATCH"] = patch_id
    settings["OS_TEST_ISSUES"] = pr
    settings["OS_TEST_TEMPLATE"] = os_test_template
    # openSUSE:Maintenance key
    settings["IMPORT_GPG_KEYS"] = "gpg-pubkey-b3fd7e48-5549fd0f"
    settings["ZYPPER_ADD_REPO_PREFIX"] = "staged-updates"

    settings["INSTALL_PACKAGES"] = " ".join(packages.keys())
    settings["VERIFY_PACKAGE_VERSIONS"] = " ".join(
        [f"{p.name} {p.version}-{p.release}" for p in packages.values()]
    )

    return settings


def get_staged_update_name(obs_project):
    query = {"deleted": 0}
    url = osc.core.makeurl(BS_HOST, ("source", obs_project), query=query)
    root = ET.parse(osc.core.http_GET(url)).getroot()
    source_packages = [n.attrib["name"] for n in root.findall("entry")]
    packages = []
    for package in source_packages:
        if package.startswith("patchinfo"):
            continue
        else:
            packages.append(package)

    # In theory every staged update, has a single package
    if len(packages) > 1:
        shortest = min((s for s in packages if ":" not in s), key=len)
        return shortest
    elif len(packages) == 0:
        raise NoSourcePackagesError("No packages detected")
    else:
        # this is in case we need to look for the package with the
        # shortest name in a given update
        return packages[0]


def get_obs_values(project, branch, pr_id):
    log.debug("Prepare obs url")
    project_template = CONFIG_DATA[project]["repo_template"]
    os_test_template = CONFIG_DATA[project]["OS_TEST_TEMPLATE"]

    # Version string has to be extracted from branch name
    branch_version = _extract_version_from_branch(branch)
    if not branch_version:
        log.error(f"Could not get version from {branch}")
        return None, None, None

    obs_project = project_template.format(
        version=branch_version, project=project, pr_id=pr_id
    )
    target_repo = REPO_PREFIX + "/"
    target_repo += obs_project.replace(":", ":/")

    os_test_template_setting = REPO_PREFIX + "/"
    os_test_template_setting += os_test_template.format(version=branch_version)

    log.info(f"Target project {obs_project}, {target_repo}, {os_test_template_setting}")
    return obs_project, target_repo, os_test_template_setting


VERSION_PATTERN = re.compile(r"(\d+\.\d+)")


def _extract_version_from_branch(branch):
    matches = VERSION_PATTERN.search(branch)
    if matches:
        return matches.group(0)

    # Return None to signal that no version was found
    return None


def get_packages_from_obs_project(obs_project):
    log.debug("Query packages in obs")
    packages = dict()
    # repository = osc api /build/{obs_project}
    # arches = osc api /build/{obs_project}/standard
    # arch = osc api /build/{obs_project}/standard/{arch}
    # for arch in arches:
    #   packages = osc api /build/{obs_project}/{repo}/{arch}/_repository?nosource=1
    #   for package in packages:
    #     get_package_deails = osc api /build/{obs_project}/standard/aarch64/_repository/opi.rpm?view=fileinfo

    repo = "standard"
    # osc api /build/{obs_project}/standard
    url = osc.core.makeurl(BS_HOST, ("build", obs_project, repo))
    root = ET.parse(osc.core.http_GET(url)).getroot()
    for arch in [n.attrib["name"] for n in root.findall("entry")]:
        query = {"nosource": 1}
        # packages/binary = osc api /build/{obs_project}/{repo}/{arch}/_repository?nosource=1
        url = osc.core.makeurl(
            BS_HOST, ("build", obs_project, repo, arch, "_repository"), query=query
        )
        root = ET.parse(osc.core.http_GET(url)).getroot()

        for binary in root.findall("binary"):
            b = binary.attrib["filename"]
            if b.endswith(".rpm"):
                # get_package_deails = osc api /build/{obs_project}/standard/aarch64/_repository/opi.rpm?view=fileinfo
                p = get_package_details(obs_project, repo, arch, b)
                packages[p.name] = p

    return packages


Package = namedtuple("Package", ("name", "version", "release"))


def get_package_details(prj, repo, arch, binary):
    url = osc.core.makeurl(
        BS_HOST,
        ("build", prj, repo, arch, "_repository", binary),
        query={"view": "fileinfo"},
    )
    root = ET.parse(osc.core.http_GET(url)).getroot()
    return Package(
        root.find(".//name").text,
        root.find(".//version").text,
        root.find(".//release").text,
    )


def gitea_query_pr(project, pr_id):
    log.debug("============== gitea_query_pr")
    pull_request_url = GITEA_HOST + f"/api/v1/repos/{project}/pulls/{pr_id}"
    return request_get(pull_request_url)


def gitea_post_status(statuses_url, job_url):
    log.debug("============== gitea_post_status")
    payload = {
        "context": "qam-openqa",
        "description": "openQA check",
        "state": "pending",
        "target_url": job_url,
    }
    request_post(statuses_url, payload)


def gitea_post_build_overview(project, pr_id, job_url):
    log.debug("============== gitea_post_build_overview")
    comment_url = GITEA_HOST + f"/api/v1/repos/{project}/issues/{pr_id}/comments"
    payload = {
        "body": f"openQA tests triggered: {job_url}",
    }
    request_post(comment_url, payload)


def gitea_post_openqa_review(project, pr_id, msg):
    log.debug("============== gitea_post_openqa_review")
    comment_url = GITEA_HOST + f"/api/v1/repos/{project}/issues/{pr_id}/comments"
    payload = {
        "body": msg,
    }
    request_post(comment_url, payload)


def gitea_get_review(project, pr_id, review_id):
    log.debug("============== gitea_get_review")
    review_url = (
        GITEA_HOST + f"/api/v1/repos/{project}/pulls/{pr_id}/reviews/{review_id}"
    )
    return request_get(review_url)


def gitea_list_reviews(project, pr):
    log.debug("============== gitea_get_review")
    review_url = (
        GITEA_HOST + f"/api/v1/repos/{project}/pulls/{pr_id}/reviews/{review_id}"
    )
    return request_get(review_url)


def get_events_by_timeline(project, pr_id, bs_bot):
    log.debug("============== get_events_by_timeline")
    limit = 50
    page = 1
    timeline = []

    while True:
        url = (
            GITEA_HOST
            + f"/api/v1/repos/{project}/issues/{pr_id}/timeline?limit={limit}&page={page}"
        )
        request = request_get(url)

        if not request:
            break

        timeline.extend(request)

        if len(request) < limit:
            break

        page += 1

    timeline.reverse()

    build_review_id = None
    openqa_build_overview = None
    previous_review = None
    ignore_last_review = False
    force_new_build = False

    for event in timeline:
        if event["type"] == "pull_push":
            log.debug(
                f"*** All events since last push ({event['body']}) have been processed for {project}#{pr_id}"
            )
            break

        user_login = event.get("user", {}).get("login")
        event_type = event.get("type")

        if event_type == "review" and user_login == bs_bot and not build_review_id:
            build_review_id = event.get("review_id")
        elif event_type == "comment":
            body = event.get("body", "")
            if user_login == MYSELF and not openqa_build_overview:
                match = re.search(r"https?://[^\s]+/tests/overview\?[^\s]+", body)
                if match:
                    log.info(f"openQA build url found {match.group(0)}")
                    log.debug(f"openQA build url found '{body}'")
                    openqa_build_overview = match.group(0)
                    if re.search(f"{REVIEW_GROUP}:\\s*(.*)", body):
                        previous_review = body
            if not force_new_build and body.strip("@ ") == MYSELF:
                force_new_build = True
        elif event_type == "review_request" and not ignore_last_review:
            if f"@{event.get('assignee', {}).get('username')}" == REVIEW_GROUP:
                ignore_last_review = True

    return build_review_id, openqa_build_overview, previous_review, ignore_last_review, force_new_build


def request_post(url, payload):
    log.debug(f"Posting request to gitea for {url}")
    log.debug(payload)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": "token " + GITEA_TOKEN,
    }
    if dry_run:
        log.debug(f"would send request to {url} with {payload}")
    else:
        try:
            content = requests.post(url, headers=headers, data=payload)
            content.raise_for_status()
        except requests.exceptions.RequestException as e:
            log.error("Error while fetching %s: %s" % (url, str(e)))
            raise (e)


def request_get(url):
    log.debug(f"Sending request to gitea for {url}")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": "token " + GITEA_TOKEN,
    }

    try:
        content = requests.get(url, headers=headers)
        content.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Error while fetching %s: %s" % (url, str(e)))
        raise (e)
    json_data = content.json()
    return json_data


def prepare_openqa_job_params(args, obs_project, data, settings):
    log.debug("create_openqa_job_params")
    statuses_url = (
        GITEA_HOST
        + f"/api/v1/repos/{data['head']['repo']['full_name']}/statuses/{data['head']['sha']}"
    )
    params = {
        "PRIO": "100",
        # add "target URL" for the "Details" button of the CI status
        "CI_TARGET_URL": args.openqa_host,
        # set Gitea parameters so the Minion job will be able to report the status back to Gitea
        "GITEA_REPO": data["head"]["repo"]["full_name"],
        "GITEA_SHA": data["head"]["sha"],
        "GITEA_STATUSES_URL": statuses_url,
        "GITEA_PR_URL": data["html_url"],
        "webhook_id": "gitea:pr:" + str(data["number"]),
        "VERSION": _extract_version_from_branch(data["base"]["label"]),
        "DISTRI": "opensuse",  # there must be a better way than to hardcode
        "FLAVOR": "staged-Updates",
        "ARCH": "x86_64",
    }
    return params | settings


def openqa_schedule(args, params):
    log.debug("============== openqa_schedule")

    if not openqa_dry_run:
        openqa.openqa_request("POST", "isos", data=params, retries=1)

    query_parameters = {
        "build": params["BUILD"],
        "distri": params["DISTRI"],
        "version": params["VERSION"],
    }

    base_url = urlparse(args.openqa_host + "/tests/overview")
    query_string = urlencode(query_parameters)
    test_overview_url = urlunparse(base_url._replace(query=query_string))
    return test_overview_url


class NoSourcePackagesError(Exception):
    pass


if __name__ == "__main__":
    args = parse_args()

    token_file_path = os.environ.get("GITEA_TOKEN_FILE")
    gitea_token = None

    if token_file_path:
        try:
            with open(token_file_path, "r") as f:
                gitea_token = f.read().strip()
        except (IOError, FileNotFoundError) as e:
            raise RuntimeError(
                f"Error reading GITEA_TOKEN_FILE '{token_file_path}': {e}"
            )
    else:
        gitea_token = os.environ.get("GITEA_TOKEN")

    if not gitea_token:
        raise RuntimeError(
            "Environment variable GITEA_TOKEN or GITEA_TOKEN_FILE must be set"
        )

    GITEA_TOKEN = gitea_token
    GITEA_HOST = args.gitea
    BS_HOST = args.bs
    REPO_PREFIX = args.repo_prefix
    REVIEW_GROUP = args.review_group
    MYSELF = args.myself
    osc.conf.get_config()
    openqa = OpenQA_Client(server=args.openqa_host)
    if args.pr_id:
        process_pull_request(args.pr_id, args)
    else:
        process_project(args)
