import os
import csv
import shutil
import logging
import argparse
import subprocess
import math
import functools
import itertools
import json
import uuid
import zipfile

import requests
from joblib import Parallel, delayed

from buildstockbatch.base import BuildStockBatchBase


class PeregrineBatch(BuildStockBatchBase):

    def __init__(self, project_filename):
        super().__init__(project_filename)
        output_dir = self.output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        logging.debug('Output directory = {}'.format(output_dir))

        singularity_image_path = os.path.join(output_dir, 'openstudio.simg')
        if not os.path.isfile(singularity_image_path):
            logging.debug('Downloading singularity image')
            simg_url = \
                'https://s3.amazonaws.com/openstudio-builds/{ver}/OpenStudio-{ver}.{sha}-Singularity.simg'.format(
                    ver=self.OS_VERSION,
                    sha=self.OS_SHA
                )
            r = requests.get(simg_url, stream=True)
            with open(singularity_image_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
            logging.debug('Downloaded singularity image to {}'.format(singularity_image_path))

    @property
    def output_dir(self):
        output_dir = self.cfg.get(
            'output_directory',
            os.path.join('/scratch/{}'.format(os.environ['USER']), os.path.basename(self.project_dir))
        )
        return output_dir

    @property
    def weather_dir(self):
        weather_dir = os.path.join(self.output_dir, 'weather')
        if not os.path.exists(weather_dir):
            os.makedirs(weather_dir)
        return weather_dir

    def run_sampling(self):
        logging.debug('Sampling')
        args = [
            'singularity',
            'exec',
            '--contain',
            '-B', '{}:/outdir'.format(self.output_dir),
            '--home', self.buildstock_dir,
            'openstudio.simg',
            'ruby',
            'resources/run_sampling.rb',
            '-p', self.cfg['project_directory'],
            '-n', str(self.cfg['baseline']['n_datapoints']),
            '-o', 'buildstock.csv'
        ]
        subprocess.run(args, check=True, env=os.environ, cwd=self.output_dir)
        destination_filename = os.path.join(self.output_dir, 'buildstock.csv')
        if os.path.exists(destination_filename):
            os.remove(destination_filename)
        shutil.move(
            os.path.join(self.buildstock_dir, 'resources', 'buildstock.csv'),
            destination_filename
        )
        return destination_filename

    def run_batch(self, n_jobs=40, nodetype='haswell', queue='batch-h'):
        self.run_sampling()
        n_datapoints = self.cfg['baseline']['n_datapoints']
        n_sims = n_datapoints * len(self.cfg.get('upgrades', []))

        # This is the maximum number of jobs we'll submit for this batch
        n_sims_per_job = math.ceil(n_sims / n_jobs)
        # Have at least 48 simulations per job
        n_sims_per_job = max(n_sims_per_job, 48)

        nodes_per_nodetype = {
            '16core': 16,
            '64GB': 24,
            '256GB': 16,
            '24core': 24,
            'haswell': 24
        }

        # Estimated 3 minutes per simulation
        walltime = math.ceil(n_sims_per_job / nodes_per_nodetype[nodetype]) * 10 * 60

        baseline_sims = zip(range(1, n_datapoints + 1), itertools.repeat(None))
        upgrade_sims = itertools.product(range(1, n_datapoints + 1), range(len(self.cfg.get('upgrades', []))))
        all_sims = itertools.chain(baseline_sims, upgrade_sims)

        for i in itertools.count(1):
            batch = list(itertools.islice(all_sims, n_sims_per_job))
            if not batch:
                break
            logging.info('Queueing job {} ({} simulations)'.format(i, len(batch)))
            job_json_filename = os.path.join(self.output_dir, 'job{:03d}.json'.format(i))
            with open(job_json_filename, 'w') as f:
                json.dump({
                    'job_num': i,
                    'batch': batch,
                }, f, indent=4)

            here = os.path.dirname(os.path.abspath(__file__))
            peregrine_sh = os.path.join(here, 'peregrine.sh')
            args = [
                'qsub',
                '-v', 'PROJECTFILE={},JOBJSON={}'.format(self.project_filename, job_json_filename),
                '-q', queue,
                '-l', 'feature={}'.format(nodetype),
                '-l', 'walltime={}'.format(walltime),
                '-o', os.path.join(self.output_dir, 'job{:03d}.out'.format(i)),
                peregrine_sh
            ]
            env = {}
            env.update(os.environ)
            env.update({
                'JOBJSON': job_json_filename,
                'PROJECTFILE': self.project_filename
            })
            resp = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env
            )
            try:
                resp.check_returncode()
            except subprocess.CalledProcessError as ex:
                print(ex.stderr)
                raise

    def run_job_batch(self, job_json_filename):
        with open(job_json_filename, 'r') as f:
            args = json.load(f)

        run_building_d = functools.partial(
            delayed(self.run_building),
            self.project_dir,
            self.buildstock_dir,
            self.weather_dir,
            self.output_dir,
            self.cfg
        )
        with Parallel(n_jobs=-1, verbose=8) as parallel:
            parallel(itertools.starmap(run_building_d, args['batch']))

    @classmethod
    def run_building(cls, project_dir, buildstock_dir, weather_dir, output_dir, cfg, i, upgrade_idx=None):
        sim_id = str(uuid.uuid4())

        # Create the simulation directory
        sim_dir = os.path.join(output_dir, 'results', sim_id)
        os.makedirs(sim_dir)

        # Generate the osw for this simulation
        osw = cls.create_osw(sim_id, cfg, i, upgrade_idx)
        with open(os.path.join(sim_dir, 'in.osw'), 'w') as f:
            json.dump(osw, f, indent=4)

        # Find and copy the weather file we'll be using for this simulation so we don't have to copy all of them.
        epw_file = None
        with open(os.path.join(output_dir, 'buildstock.csv'), 'r', newline='') as f:
            cf = csv.DictReader(f)
            for row in cf:
                if int(row['Building']) == i:
                    epw_file = os.path.join(weather_dir, row['Location EPW'])
                    assert(os.path.isfile(epw_file))
                    break
        weather_dest = os.path.join(sim_dir, 'weather')
        os.makedirs(weather_dest)
        shutil.copy(os.path.join(weather_dir, 'Placeholder.epw'), weather_dest)
        shutil.copy(epw_file, weather_dest)

        # Copy other necessary stuff into the simulation directory
        dirs_to_copy = [
            (os.path.join(buildstock_dir, 'measures'), os.path.join(sim_dir, 'measures')),
            (os.path.join(buildstock_dir, 'resources'), os.path.join(sim_dir, 'lib', 'resources')),
            (os.path.join(project_dir, 'housing_characteristics'), os.path.join(sim_dir, 'lib', 'housing_characteristics')),
            (os.path.join(project_dir, 'seeds'), os.path.join(sim_dir, 'seeds'))
        ]
        for src, dest in dirs_to_copy:
            shutil.copytree(src, dest)

        # Call singularity to run the simulation
        args = [
            'singularity', 'exec',
            '--contain',
            '--pwd', '/var/simdata/openstudio',
            '-B', '{}:/var/simdata/openstudio'.format(sim_dir),
            'openstudio.simg',
            'openstudio',
            'run',
            '-w', 'in.osw'
        ]
        logging.debug(' '.join(args))
        with open(os.path.join(sim_dir, 'singularity_output.log'), 'w') as f_out:
            subprocess.run(args, check=True, stdout=f_out, stderr=subprocess.STDOUT, cwd=output_dir)

        # Clean up the stuff we copied
        for dir in dirs_to_copy:
            shutil.rmtree(dir[1])
        shutil.rmtree(os.path.join(sim_dir, 'lib'))
        shutil.rmtree(os.path.join(sim_dir, 'weather'))

        # Remove files already in data_point.zip
        zipfilename = os.path.join(sim_dir, 'run', 'data_point.zip')
        if os.path.isfile(zipfilename):
            with zipfile.ZipFile(zipfilename, 'r') as zf:
                for filename in zf.namelist():
                    filepath = os.path.join(sim_dir, 'run', filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)


def main():
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument('project_filename')
    args = parser.parse_args()
    batch = PeregrineBatch(args.project_filename)
    job_json_filename = os.environ.get('JOBJSON')
    if job_json_filename:
        batch.run_job_batch(job_json_filename)
    else:
        batch.run_batch()


if __name__ == '__main__':
    main()
