import requests
import re
from unittest.mock import patch, MagicMock

from buildstockbatch.localdocker import LocalDockerBatch


def test_docker_image_exists_on_docker_hub(basic_residential_project_file):
    project_filename, results_dir = basic_residential_project_file()
    with patch.object(LocalDockerBatch, 'weather_dir', None):
        local_docker_batch = LocalDockerBatch(project_filename)
        docker_image, docker_tag = local_docker_batch.docker_image.split(':')
        baseurl = 'https://registry.hub.docker.com/v2/'
        r1 = requests.get(baseurl)
        assert(r1.status_code == 401)
        m = re.search(r'realm="(.+?)"', r1.headers['Www-Authenticate'])
        authurl = m.group(1)
        m = re.search(r'service="(.+?)"', r1.headers['Www-Authenticate'])
        service = m.group(1)
        r2 = requests.get(authurl, params={
            'service': service,
            'scope': f'repository:{docker_image}:pull'
        })
        assert(r2.ok)
        token = r2.json()['token']
        r3 = requests.head(
            f'{baseurl}{docker_image}/manifests/{docker_tag}',
            headers={'Authorization': f'Bearer {token}'}
        )
        assert(r3.ok)
