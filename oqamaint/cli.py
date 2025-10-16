# -*- coding: utf-8 -*-

# standard library
import logging
import os.path as opa
import json
import sys
import cmdln
# external dependency
from openqa_client.client import OpenQA_Client

# from package itself
import osc
from oqamaint.openqabot import OpenQABot
from oqamaint.opensuse import openSUSEUpdate
import ReviewBot
from oqamaint.suse import SUSEUpdate
from oqamaint.gitea import Gitea


class CommandLineInterface(ReviewBot.CommandLineInterface):

    def __init__(self, *args, **kwargs):
        super().__init__(self, *args, **kwargs)
        self.clazz = OpenQABot
        self.gitea = None

    def get_optparser(self):
        parser = ReviewBot.CommandLineInterface.get_optparser(self)
        parser.add_option("--force", action="store_true",
                          help="recheck requests that are already considered done")
        parser.add_option("--no-comment", dest='comment', action="store_false",
                          default=True, help="don't actually post comments to obs")
        parser.add_option("--openqa", metavar='HOST', help="openqa api host")
        parser.add_option(
            "--data",
            default=opa.abspath(
                opa.dirname(
                    sys.argv[0])),
            help="Path to metadata dir (data/*.json)")
        parser.add_option("--git", help="Enable git backend", dest="git")
        return parser

    def _load_metadata(self):
        path = self.options.data
        project = {}

        with open(opa.join(path, "data/repos.json"), 'r') as f:
            target = json.load(f)

        with open(opa.join(path, "data/apimap.json"), 'r') as f:
            api = json.load(f)

        with open(opa.join(path, "data/incidents.json"), 'r') as f:
            for i, j in json.load(f).items():
                if i.startswith('SUSE'):
                    project[i] = SUSEUpdate(j)
                elif i.startswith('openSUSE'):
                    project[i] = openSUSEUpdate(j)
                else:
                    raise Exception("Unknown openQA", i)
        return project, target, api

    def postoptparse(self):
        # practically quiet
        level = logging.WARNING
        if (self.options.debug):
            level = logging.DEBUG
        elif (self.options.verbose):
            # recomended variant
            level = logging.INFO

        self.logger = logging.getLogger(self.optparser.prog)
        self.logger.setLevel(level)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(levelname)-2s: %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

        osc.conf.get_config(override_apiurl=self.options.apiurl)

        if (self.options.osc_debug):
            osc.conf.config['debug'] = 1

        if self.options.config:
            self.checker.load_config(self.options.config)

        if self.options.review_mode:
            self.checker.review_mode = self.options.review_mode

        if self.options.fallback_user:
            self.checker.fallback_user = self.options.fallback_user

        if self.options.fallback_group:
            self.checker.fallback_group = self.options.fallback_group

        if self.options.git:
            # self.checker.gitea = Gitea("https://src.opensuse.org")
            self.gitea = Gitea(self.options.git)

        self.checker = self.setup_checker()

    def setup_checker(self):
        bot = ReviewBot.CommandLineInterface.setup_checker(self)

        if self.options.force:
            bot.force = True

        bot.do_comments = self.options.comment

        if not self.options.openqa:
            raise osc.oscerr.WrongArgs("missing openqa url")

        bot.openqa = OpenQA_Client(server=self.options.openqa)
        project, target, api = self._load_metadata()
        bot.api_map = api
        bot.tgt_repo = target
        bot.project_settings = project
        if self.options.git:
            bot.gitea = self.gitea
        return bot

    @cmdln.option('-p', '--project', metavar="PROJECT", type="string", help="Project to check, e.g openSUSE/Leap")
    @cmdln.option('-b', '--branch', metavar="branch", type="string", help="branch in the project, e.g leap-16.0")
    @cmdln.option('--obs-bot', metavar="build_bot_username", type="string",
                  help="Bot that notifies build result from obs, eg: autogits_obs_staging_bot")
    def do_santiago(self, subcmd, opts, *args):
        """${cmd_name}: Check open Pull request against a project

        ${cmd_usage}
        ${cmd_option_list}
        """

        self.checker.setup_open_pr(opts.project, opts.branch, opts.obs_bot)

        for request in self.checker.requests:
            self.logger.info(f"Working on {request}")
            if self.checker.has_build_finished(request):
                self.logger.debug(f"Build for {request} has finished")
                test_result = self.checker.terminamos_los_tests(opts.project, request)
                if test_result is None:  # fingiremos que no estan listos con un None
                    self.logger.info("No hay tests para este proyecto, disparandolos")
                    self.checker.dispara_los_tests_para_projecto_pr(opts.project, request)
            else:
                self.logger.info(f"el build no esta listo... saltando {request}")
                continue

            if test_result == "PASSED":
                self.logger.info("Tests verdes, marcando en gitea")
                mensaje_para_gitea = "va querer copia?"
                self.checker.comentamos_en_gitea(opts.project, request, mensaje_para_gitea)

                self.logger.info("A-Probado")
                self.checker.marcarcamos_el_check_en_gitea(opts.project, request, True)
            else:
                self.logger.info("Tests rojos, marcando en gitea")
                mensaje_para_gitea = "Saldo insuficiente chamo"
                self.checker.comentamos_en_gitea(opts.project, request, mensaje_para_gitea)

                self.logger.info("A-plazado")
                self.checker.marcarcamos_el_check_en_gitea(opts.project, request, False)

            self.logger.info(f"DONE with open pull requests for {opts.project}")
