#!/bin/bash
#PBS -l nodes=1
#PBS -l walltime=1:00:00
#PBS -q short
#PBS -A res_stock
#PBS -j oe

module load conda
conda remove -y --name buildstock --all
conda create -y --name buildstock python=3.6 pandas
source activate buildstock
pip install --upgrade pip
pip install -e .
