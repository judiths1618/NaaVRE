import os
from github3 import login
import tarfile
import copy
from pathlib import Path
import json
import shutil
from requests.models import HTTPBasicAuth
import yaml
import uuid
import requests
from jupyterlab_vre.github.gh_credentials import GHCredentials
import nbformat as nb
import autopep8
from notebook.base.handlers import APIHandler
from tornado import web
from datetime import datetime, timedelta
from jupyterlab_vre.extractor.extractor import Extractor
from jupyterlab_vre.converter.converter import ConverterReactFlowChart
from jupyterlab_vre.sdia.sdia import SDIA
from jupyterlab_vre.sdia.sdia_credentials import SDIACredentials
from jupyterlab_vre.faircell import Cell
from jupyterlab_vre.storage.catalog import Catalog
from jupyterlab_vre.storage.azure import AzureStorage
from jupyterlab_vre.workflows.parser import WorkflowParser
from jinja2 import Environment, PackageLoader, FileSystemLoader

################################################################################

                            # Extraction

################################################################################

class ExtractorHandler(APIHandler, Catalog):

    @web.authenticated
    async def get(self):
        msg_json = dict(title="Operation not supported.")
        self.write(msg_json)
        self.flush()


    @web.authenticated
    async def post(self, *args, **kwargs):
        payload = self.get_json_body()
        cell_index = payload['cell_index']
        notebook = nb.reads(json.dumps(payload['notebook']), nb.NO_CONVERT)
        extractor = Extractor(notebook)

        source = notebook.cells[cell_index].source
		
        title = source.partition('\n')[0]
        title = title.replace('#', '').strip() if title[0] == "#" else "Untitled"
        
        ins = set(extractor.infere_cell_inputs(source))
        outs = set(extractor.infere_cell_outputs(source))
        params = []
        confs = extractor.extract_cell_conf_ref(source)

        dependencies = extractor.infere_cell_dependencies(source)

        cell = Cell(
            title               = title,
            task_name           = title.lower().replace(' ', '-'),
            original_source     = source,
            inputs              = ins,
            outputs             = outs,
            params              = params,
            confs               = confs,
            dependencies        = dependencies,
            container_source    = ""
        )

        cell.integrate_configuration()
        params = list(extractor.extract_cell_params(cell.original_source))
        cell.params = params

        node_id = str(uuid.uuid4())[:7]
        node = ConverterReactFlowChart.get_node(
            node_id, 
            title, 
            ins, 
            outs, 
            params, 
            dependencies
        )

        print(dependencies)

        chart = {
            'offset': {
                'x': -100,
                'y': 0,
            },
            'scale': 1,
            'nodes': { node_id: node },
            'links': {},
            'selected': {},
            'hovered': {},
        }

        cell.node_id = node_id
        cell.chart_obj = chart

        Catalog.editor_buffer = copy.deepcopy(cell)

        self.write(json.dumps({
            'node_id'   : node_id,
            'chart'     : chart,
            'deps'      : dependencies
        }))
        
        self.flush()


################################################################################

                            # Types

################################################################################

class TypesHandler(APIHandler, Catalog):

    @web.authenticated
    async def post(self, *args, **kwargs):

        payload = self.get_json_body()
        port = payload['port']
        p_type = payload['type']
        cell = Catalog.editor_buffer
        cell.types[port] = p_type

################################################################################

                            # Catalog

################################################################################

class CellsHandler(APIHandler, Catalog):

    @web.authenticated
    async def get(self):
        msg_json = dict(title="Operation not supported.")
        self.write(msg_json)
        self.flush()


    @web.authenticated
    async def post(self, *args, **kwargs):

        current_cell = Catalog.editor_buffer
        deps = current_cell.generate_dependencies()
        confs = current_cell.generate_configuration()
        current_cell.clean_code()
        loader = PackageLoader('jupyterlab_vre', 'templates')
        template_env = Environment(loader=loader, trim_blocks=True, lstrip_blocks=True)
        
        template_cell = template_env.get_template('cell_template.jinja2')
        template_dockerfile = template_env.get_template('dockerfile_template_conda.jinja2')
        template_conda = template_env.get_template('conda_env_template.jinja2')

        compiled_code = template_cell.render(cell=current_cell, deps=deps, types=current_cell.types, confs=confs)
        compiled_code = autopep8.fix_code(compiled_code)
        current_cell.container_source = compiled_code

        Catalog.add_cell(current_cell)

        cells_path = os.path.join(str(Path.home()), 'NaaVRE', 'cells')

        if not os.path.exists(cells_path):
            os.mkdir(cells_path)

        cell_path = os.path.join(cells_path, current_cell.task_name)

        if os.path.exists(cell_path):
            for files in os.listdir(cell_path):
                path = os.path.join(cell_path, files)
                os.remove(path)

        else:
            os.mkdir(cell_path)

        cell_file_name = current_cell.task_name + '.py'
        dockerfile_name = 'Dockerfile.qcdis.' + current_cell.task_name
        env_name = current_cell.task_name + '-environment.yaml'

        set_deps = set([dep['module'].split('.')[0] for dep in current_cell.dependencies])

        cell_file_path = os.path.join(cell_path, cell_file_name)
        dockerfile_file_path = os.path.join(cell_path, dockerfile_name)
        env_file_path = os.path.join(cell_path, env_name)
        files_info = {}
        files_info[cell_file_name]  = cell_file_path
        files_info[dockerfile_name] = dockerfile_file_path
        files_info[env_name] = env_file_path

        template_cell.stream(cell=current_cell, deps=deps, types=current_cell.types, confs=confs).dump(cell_file_path)
        template_dockerfile.stream(task_name=current_cell.task_name).dump(dockerfile_file_path)
        template_conda.stream(deps=list(set_deps)).dump(os.path.join(cell_path, env_name))

        token = Catalog.get_gh_token()
        gh = login(token=token['token'])
        repository = gh.repository('QCDIS', 'NaaVRE-container-prestage')

        last_comm = next(repository.commits(number = 1), None)

        if last_comm:
            last_tree_sha = last_comm.commit.tree.sha
            tree = repository.tree(last_tree_sha)
            paths = []

            for comm_file in tree.tree:
                paths.append(comm_file.path)

        if current_cell.task_name in paths:
            print('Cell is already in repository')
            # TODO: Update file
        else:
            print('Cell is not in repository')
            for f_name, f_path in files_info.items():
                with open(f_path, 'rb') as f:
                    content = f.read()
                    repository.create_file(
                        path        = current_cell.task_name + '/' + f_name,
                        message     = current_cell.task_name + ' creation',
                        content     = content,
                    )

        self.flush()
    

    @web.authenticated
    async def delete(self, *args, **kwargs):
        payload = self.get_json_body()
        Catalog.delete_cell_from_title(payload['title'])


class CatalogGetAllHandler(APIHandler, Catalog):

    @web.authenticated
    async def get(self):
        self.write(json.dumps(Catalog.get_all_cells()))
        self.flush()


    @web.authenticated
    async def post(self, *args, **kwargs):
        msg_json = dict(title="Operation not supported.")
        self.write(msg_json)
        self.flush()



################################################################################

                            # SDIA Auth

################################################################################

class SDIAAuthHandler(APIHandler, SDIA, Catalog):

    @web.authenticated
    async def post(self, *args, **kwargs):
        
        payload = self.get_json_body()
        reply = {}
        res = SDIA.test_auth(payload['sdia-auth-username'], payload['sdia-auth-password'], payload['sdia-auth-endpoint'])
        error = issubclass(type(res), Exception)

        if not error:
            Catalog.add_credentials(
                SDIACredentials(
                    username = payload['sdia-auth-username'], 
                    password = payload['sdia-auth-password'],
                    endpoint = payload['sdia-auth-endpoint']
                )
            )

        reply['message'] = str(res) if error else 'Credentials Saved'
        self.write(reply)
        self.flush()


################################################################################

                            # Github  Auth

################################################################################


class GithubAuthHandler(APIHandler, Catalog):

    @web.authenticated
    async def post(self, *args, **kwargs):

        payload = self.get_json_body()
        Catalog.add_gh_credentials(
            GHCredentials(token = payload['github-auth-token'])
        )
        self.flush()


################################################################################

                            # SDIA Credentials

################################################################################


class SDIACredentialsHandler(APIHandler, Catalog):

    @web.authenticated
    async def get(self, *args, **kwargs):
        self.write(json.dumps(Catalog.get_credentials()))
        self.flush()


################################################################################

                            # Automator

################################################################################


class ProvisionAddHandler(APIHandler, Catalog, SDIA):

    @web.authenticated
    async def post(self, *args, **kwargs):

        payload = self.get_json_body()
        cred_username = payload['credential']
        template_id = payload['provision_template']
        credentials = Catalog.get_credentials_from_username(cred_username)

        resp = SDIA.provision(credentials, template_id)
        print(resp)

        self.flush()


################################################################################

                            # Workflows

################################################################################


class ExportWorkflowHandler(APIHandler):

    @web.authenticated
    async def post(self, *args, **kwargs):

        payload = self.get_json_body()
        global_params = []

        nodes = payload['nodes']
        links = payload['links']

        parser = WorkflowParser(nodes, links)
        cells = parser.get_workflow_cells()
        deps_dag = parser.get_dependencies_dag()

        for nid, cell in cells.items():
            global_params.extend(cell['params'])

        loader = PackageLoader('jupyterlab_vre', 'templates')
        template_env = Environment(loader=loader, trim_blocks=True, lstrip_blocks=True)
        template = template_env.get_template('workflow_template.jinja2')
        template.stream(deps_dag=deps_dag, cells=cells, global_params=set(global_params)).dump('workflow.yaml')
        self.flush()
        

