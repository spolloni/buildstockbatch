#!/bin/bash

aws s3 cp s3://buildstockbatch-test/emr/bsb_post.py bsb_post.py
/home/hadoop/miniconda/bin/python bsb_post.py