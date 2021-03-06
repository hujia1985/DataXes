#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on 2018年9月14日

@author: Rassyan
"""

import yaml
import json
from yaml import SafeLoader
from datetime import datetime
import os
import logging
import sys
import subprocess
from prettytable import PrettyTable

sys.path.append("/opt/datax/bin/")
import datax
from elasticsearch import Elasticsearch

FULL_DATA_JOBS = "full_data_jobs"
INCR_DATA_JOBS = "incr_data_jobs"

DATAXES_INDEX = ".dataxes_run_history"
DATAXES_TYPE = "_doc"
DATAXES_DIR = "/opt/datax/"

STATUS_SUCCESS = "SUCCESS"
STATUS_FAIL = "FAIL"
STATUS_RUNNING = "RUNNING"


def construct_yaml_str(self, node):
    # Override the default string handling function
    # to always return unicode objects
    value = self.construct_scalar(node)
    try:
        return value.encode('utf8')
    except UnicodeEncodeError:
        return value


SafeLoader.add_constructor(u'tag:yaml.org,2002:str', construct_yaml_str)


class DataXes:
    def __init__(self, config_path):
        self.print_logo()
        self.job_start_time = datetime.now().replace(microsecond=0)
        with open(config_path) as stream:
            self.config = yaml.safe_load(stream)

        self.client = Elasticsearch(self.es_hosts(), sniff_on_start=True, sniff_on_connection_fail=True,
                                    sniffer_timeout=60)
        self.job_dir = self.work_dir("job")
        self.log_dir = self.work_dir("log")

        self.assert_job_status()

        self.end_time = self.get_end_time()
        self.start_time = self.get_start_time()

        self.assert_job_time()

        self.log_init()

        self.job_name = self.dataxes_job_name()
        self.job_type = ""
        self.status = STATUS_RUNNING
        self.datax_jobs = []
        self.template = """{}"""

    def print_logo(self):
        print r'''==================================================='''
        print r'''________          __         ____  ___             '''
        print r'''\______ \ _____ _/  |______  \   \/  /____   ______'''
        print r''' |    |  \\__  \\   __\__  \  \     _/ __ \ /  ___/'''
        print r''' |    `   \/ __ \|  |  / __ \_/     \  ___/ \___ \ '''
        print r'''/_______  (____  |__| (____  /___/\  \___  /____  >'''
        print r'''        \/     \/          \/      \_/   \/     \/ '''
        print r'''==================================================='''

    def work_dir(self, dir_name):
        work_dir = "{}{}".format(DATAXES_DIR, dir_name)
        if os.path.isfile(work_dir):
            os.remove(work_dir)
        if not os.path.exists(work_dir):
            os.makedirs(work_dir)
        return work_dir

    def log_init(self):
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s [%(levelname)s] at %(filename)s,%(lineno)d: %(message)s',
                            datefmt='%Y-%m-%d(%a)%H:%M:%S',
                            filename=self.dataxes_log_path())
        # 将大于或等于INFO级别的日志信息输出到StreamHandler(默认为标准错误)
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(levelname)-8s] %(message)s')  # 屏显实时查看，无需时间
        console.setFormatter(formatter)
        logging.getLogger().addHandler(console)

    # 任务构建相关

    def dataxes_json_config_path(self, job_name):
        return "{}/{}.json".format(self.job_dir, job_name)

    def dataxes_log_path(self):
        return "{}/{}.log".format(self.log_dir, self.dataxes_history_id())

    def dataxes_process(self):
        return self.config.get("job", {}).get("process") or 4

    def dataxes_datetime_significance(self):
        return self.config.get("job", {}).get("datetime_significance") or '1s'

    def dataxes_datax_args(self, job_name):
        datax_args = [self.dataxes_json_config_path(job_name)]
        datax_args.extend(self.config.get("job", {}).get("datax_args") or [])
        return datax_args

    # ElasticSearch相关

    def dataxes_alias_name(self):
        ins = self.config.get("es", {}).get("index_name").split('@')
        if 1 <= len(ins) <= 2:
            return ins[0]
        else:
            raise Exception("索引名规则错误")

    def dataxes_partition_name(self):
        ins = self.config.get("es", {}).get("index_name").split('@')
        if len(ins) == 2:
            return ins[1]

    def dataxes_job_name(self):
        return self.config.get("es", {}).get("index_name")

    def dataxes_index_name(self, partition=None):
        return '@'.join((self.dataxes_partition_alias_name(partition), self.end_time.strftime("%Y%m%d%H%M%S")))

    def dataxes_partition_alias_name(self, partition=None):
        index_names = [self.dataxes_alias_name()]
        if partition:
            index_names.append(partition)
        elif self.dataxes_partition_name():
            index_names.append(self.dataxes_partition_name())
        return '@'.join(index_names)

    def dataxes_history_id(self):
        history_ids = [self.dataxes_alias_name()]
        if self.dataxes_partition_name():
            history_ids.append(self.dataxes_partition_name())
        history_ids.append(self.job_start_time.strftime("%Y%m%d%H%M%S"))
        return '@'.join(history_ids)

    def dataxes_type_name(self):
        return self.config.get("es", {}).get("type_name") or DATAXES_TYPE

    def dataxes_index_template(self):
        with open(self.config.get("es", {}).get("template_file")) as template_file:
            template = json.load(template_file)

            # 全量写入优化策略

            def _pop_config(dict_):
                pop_keys = []
                for key in dict_:
                    for s in ["refresh_interval", "number_of_replicas", "auto_expand_replicas"]:
                        if key.endswith(s):
                            pop_keys.append(key)
                for key in pop_keys:
                    dict_.pop(key)
                return dict_

            template["settings"] = _pop_config(template.get("settings", {}))
            new_setting = _pop_config(template.get("settings", {}).get("index", {}))
            new_setting["refresh_interval"] = "-1"
            new_setting["number_of_replicas"] = "0"
            template["settings"]["index"] = new_setting
            # 添加名称匹配规则
            template["index_patterns"] = ["{}@*".format(self.dataxes_job_name())]
            # 添加别名规则
            template["aliases"] = {".{}@new".format(self.dataxes_alias_name()): {}}
            # 添加模板套用优先级
            template["order"] = 1 if self.dataxes_partition_name() else 0
            return template

    def dataxes_index_settings(self):
        with open(self.config.get("es", {}).get("template_file")) as template_file:
            template = json.load(template_file)
            settings = template.get("settings", {})
            settings_index = settings.get("index", {})
            update_settings = {"refresh_interval": "1s"}
            for key, value in settings.items() + settings_index.items():
                if key.endswith("refresh_interval"):
                    update_settings["refresh_interval"] = value
                elif key.endswith("number_of_replicas"):
                    update_settings["number_of_replicas"] = value
                elif key.endswith("auto_expand_replicas"):
                    update_settings["auto_expand_replicas"] = value
            if len(update_settings.keys()) == 1:
                update_settings["auto_expand_replicas"] = "0-1"
            return {"settings": {"index": update_settings}}

    # 以下为esWriter使用的配置

    def es_hosts(self):
        return self.config.get("es", {}).get("hosts", [])

    def es_action_type(self):
        return self.config.get("es", {}).get("action_type", "index")

    def es_bulk_actions(self):
        return self.config.get("es", {}).get("bulk_actions", 5000)

    def es_bulk_size_mb(self):
        return self.config.get("es", {}).get("bulk_size_mb", 20)

    def es_retry_delay_secs(self):
        return self.config.get("es", {}).get("retry_delay_secs", 1)

    def es_max_number_of_retries(self):
        return self.config.get("es", {}).get("max_number_of_retries", 3)

    def dataxes_config(self, e, l, action_type='', t=[], lp={}):
        """
        :param e: 函数，使用self.start_time_dt和self.end_time_dt作为函数参数，返回datax支持的reader的json配置对应的dict
        :param t: list，成员为dict，形如 {"name": "xy2geo_point", "parameter": {"columnIndex": 22}}，datax的transformer插件
        :param l: list，成员为dict，形如 {"type": "id", "name": "address_id"}，用于依次描述转换json时所使用的字段
        :param lp: dict，形如 {"script": "ctx._source.num = 1;"}，用于补充或覆盖eswriter的parameter配置
        :param action_type: eswriter动作类型
        :return: datax的json配置
        """
        writer_parameter = {
            "hosts": self.es_hosts(),
            "action_type": action_type if action_type else self.es_action_type(),
            "index": self.dataxes_index_name(),
            "type": self.dataxes_type_name(),
            "bulk_actions": self.es_bulk_actions(),
            "bulk_size_mb": self.es_bulk_size_mb(),
            "retry_delay_secs": self.es_retry_delay_secs(),
            "max_number_of_retries": self.es_max_number_of_retries(),
            "column": l
        }
        if lp:
            writer_parameter.update(lp)
        return {
            "job": {
                "setting": {
                    "speed": {
                        "channel": self.dataxes_process()
                    }
                },
                "content": [
                    {
                        "reader": e(self.start_time, self.end_time),
                        "transformer": t,
                        "writer": {
                            "name": "eswriter",
                            "parameter": writer_parameter
                        }
                    }
                ]
            }
        }

    def save_dataxes_config(self, dataxes_config, job_name):
        datax_job_json_file = self.dataxes_json_config_path(job_name)
        self.datax_jobs.append(
            {"name": job_name, "config": """{}""".format(json.dumps(dataxes_config, ensure_ascii=False, indent=2))})
        with open(datax_job_json_file, 'w') as json_file:
            json.dump(dataxes_config, json_file, ensure_ascii=False, indent=2)
        logging.info("datax的job文件，已保存至{}".format(datax_job_json_file))

    def get_end_time(self):
        # 得到本次作业结束时间的offset
        end_time_dt = datetime.now().replace(microsecond=0)
        datetime_significance = self.dataxes_datetime_significance()
        significance_unit = datetime_significance[-1]
        significance_value = int(datetime_significance[:-1])
        if significance_unit == "s":
            end_time_dt = end_time_dt.replace(second=end_time_dt.second / significance_value * significance_value,
                                              microsecond=0)
        elif significance_unit == "m":
            end_time_dt = end_time_dt.replace(minute=end_time_dt.minute / significance_value * significance_value,
                                              second=0,
                                              microsecond=0)
        elif significance_unit == "h":
            end_time_dt = end_time_dt.replace(hour=end_time_dt.hour / significance_value * significance_value,
                                              minute=0,
                                              second=0,
                                              microsecond=0)
        elif significance_unit == "d":
            end_time_dt = end_time_dt.replace(day=end_time_dt.day / significance_value * significance_value,
                                              hour=0,
                                              minute=0,
                                              second=0,
                                              microsecond=0)
        else:
            raise Exception("significance_unit，支持后缀s/m/h/d，当前配置为：{}".format(datetime_significance))
        return end_time_dt

    def get_start_time(self):
        # 得到上次作业结束时间的offset
        last_history = self.search_dataxes_last_job(STATUS_SUCCESS)
        if last_history:
            last_success_time = last_history.get("end_time")
            return datetime.strptime(last_success_time, "%Y-%m-%dT%H:%M:%S")

    def assert_job_status(self):
        last_history = self.search_dataxes_last_job()
        if last_history:
            status_ = last_history.get("status")
            if status_ == STATUS_RUNNING:
                # TODO 判断是否已停写
                self.suicide_before_running("上次同步任务 {} 未执行完成，退出！".format(self.dataxes_job_name()))
            elif status_ == STATUS_FAIL:
                # TODO 判断失败进度 可否继续运行
                print "上次同步任务 {} 可能存在失败, 重新尝试！".format(self.dataxes_job_name())

    def assert_job_time(self):
        if self.start_time:
            if self.start_time == self.end_time:
                self.suicide_before_running("上次同步任务 {}，未到达下次同步时间，退出！".format(self.dataxes_job_name()))
            if self.start_time > self.end_time:
                self.suicide_before_running("上次同步任务 {} 晚于本次同步任务 {}，请排查服务器时间是否正确".format(
                    self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                    self.end_time.strftime("%Y-%m-%d %H:%M:%S")))

    def suicide_before_running(self, message, ret=-1):
        print message
        print "发生异常，退出！"
        sys.exit(ret)

    def record_then_suicide(self, message, ret=-1):
        logging.error(message)
        logging.error("发生异常，退出！")
        self.save_dataxes_run_history(STATUS_FAIL)
        sys.exit(ret)

    def do_jobs(self, jobs, force_full=False):
        try:
            # prepare do
            logging.info("同步任务开始执行，任务信息保存至：{}".format(self.job_dir))
            self.make_jobs(jobs, force_full)
            self.put_index_template()
            self.save_dataxes_run_history(STATUS_RUNNING)
            self._es_delete_new_candidate_indices()

            index_alias = self.index_alias_when_incr() if self.job_type == INCR_DATA_JOBS else {}
            if index_alias:
                self._es_change_aliases([{"add": {"index": i, "alias": a}} for i, a in index_alias.items()])

            for datax_job in self.datax_jobs:
                return_code, result_log = self.do_job(datax_job.get("name", ""))
                if return_code != 0:
                    datax_job["status"] = STATUS_FAIL
                    self.record_then_suicide("同步任务执行失败，请到log目录查看对应日志！", return_code)
                else:
                    logging.info("子任务[{}]执行成功".format(datax_job["name"]))
                    datax_job["result_log"] = result_log
                    datax_job["status"] = STATUS_SUCCESS
                    self.save_dataxes_run_history(STATUS_RUNNING)

            # post do
            if index_alias:
                self._es_change_aliases([{"delete": {"index": i, "alias": a}} for i, a in index_alias.items()])

            self.put_index_settings(self._es_get_candidate_indices())
            if self.job_type == FULL_DATA_JOBS:
                self.dataxes_alias_change()
            self.job_end_time = datetime.now().replace(microsecond=0)
            self.save_dataxes_run_history(STATUS_SUCCESS)
            logging.info("同步任务执行成功！")
        except Exception, err:
            self.record_then_suicide(err)

    def do_job(self, job_name):
        parser = datax.getOptionParser()
        options, args = parser.parse_args(self.dataxes_datax_args(job_name))
        start_command = datax.buildStartCommand(options, args)
        logging.info(start_command)

        try:
            child_process = subprocess.Popen(start_command, shell=True, stdout=subprocess.PIPE)
            datax.register_signal()
            buff_len = 20
            log_buff = range(buff_len)
            i = 0
            while True:
                log = child_process.stdout.readline()
                if not log and child_process.poll() is not None:
                    print ''
                    break
                else:
                    print log[:-1]
                    log_buff[i] = log[:-1]
                    i = (i + 1) % buff_len
            is_result_log = False
            result_log = ""
            for j in range(buff_len):
                if '任务启动时刻' in log_buff[(i + j) % buff_len]:
                    is_result_log = True
                if is_result_log:
                    result_log = '\n'.join((result_log, log_buff[(i + j) % buff_len].decode('utf-8')))
            return child_process.returncode, result_log
        except (KeyboardInterrupt, SystemExit):
            datax.suicide_before_running()

    def make_jobs(self, jobs, force_full):
        """
        将datax的json写入文件
        :return: datax的json配置
        """
        if self.start_time:
            logging.info("本次同步任务，起始时间为 {}".format(self.start_time.strftime("%Y-%m-%d %H:%M:%S")))
        else:
            logging.info("本次同步任务，检测到之前无运行记录！")
        logging.info("本次同步任务，截止时间为 {}".format(self.end_time.strftime("%Y-%m-%d %H:%M:%S")))
        if not force_full and self.start_time and jobs.get(INCR_DATA_JOBS, []):
            # 构造增量作业
            self.job_type = INCR_DATA_JOBS
            logging.info("本次同步任务，将进行增量同步！")
            # 在原来索引上跑增量
            n = 1
            for dataxes_config_param_tuple in jobs.get(INCR_DATA_JOBS, []):
                dataxes_config = self.dataxes_config(*dataxes_config_param_tuple)
                job_name = "{}_{}@{}".format(INCR_DATA_JOBS, n, self.dataxes_history_id())
                self.save_dataxes_config(dataxes_config, job_name)
                n += 1
        else:
            # 构造全量作业
            self.job_type = FULL_DATA_JOBS
            logging.info("本次同步任务，将进行全量同步！")
            n = 1
            for dataxes_config_param_tuple in jobs.get(FULL_DATA_JOBS, []):
                dataxes_config = self.dataxes_config(*dataxes_config_param_tuple)
                job_name = "{}_{}@{}".format(FULL_DATA_JOBS, n, self.dataxes_history_id())
                self.save_dataxes_config(dataxes_config, job_name)
                n += 1

    def put_index_template(self):
        logging.info("================ 提交索引模板 ================")
        template = self.dataxes_index_template()
        try:
            self.template = """{}""".format(json.dumps(template, indent=2))
            self.put_template = self.client.indices.put_template(self.dataxes_job_name(), template)
            if self.put_template.get("acknowledged", False):
                logging.info("提交索引模板成功")
            else:
                logging.error("提交索引模板失败")
                self.record_then_suicide("提交索引模板失败")
        except Exception, e:
            logging.error(e)
            self.record_then_suicide("提交索引模板失败")

    def put_index_settings(self, indices):
        logging.info("================ 修改索引副本数、刷新频率 ================")
        settings = self.dataxes_index_settings()
        try:
            if indices:
                self.settings = """{}""".format(json.dumps(settings, indent=2))
                self.put_settings = self.client.indices.put_settings(settings, ','.join(indices))
                if self.put_settings.get("acknowledged", False):
                    logging.info("修改索引副本数、刷新频率成功")
                else:
                    self.record_then_suicide("修改索引副本数、刷新频率失败")
            else:
                logging.error("未发现新建立的索引")
        except Exception, e:
            logging.error(e)
            self.record_then_suicide("修改索引副本数、刷新频率失败")

    def search_dataxes_last_job(self, status=None):
        self.create_dataxes_index_if_not_exists()
        filter_ = [
            {
                "term": {
                    "job_name.keyword": {
                        "value": self.dataxes_job_name()
                    }
                }
            }
        ]
        if status:
            filter_.append({
                "term": {
                    "status.keyword": {
                        "value": status
                    }
                }
            })
        response = self.client.search(DATAXES_INDEX, body={
            "query": {
                "bool": {
                    "filter": filter_
                }
            },
            "sort": [
                {
                    "end_time": {
                        "order": "desc"
                    }
                }
            ],
            "size": 1
        })
        if response.get("hits", {}).get("total", 0) > 0:
            return response.get("hits").get("hits")[0].get("_source")

    def save_dataxes_run_history(self, status):
        self.create_dataxes_index_if_not_exists()
        self.status = status
        job_history = {}
        for key in self.__dict__.keys():
            if key in ['status', 'job_name', 'job_type', 'datax_jobs', 'config',
                       'job_start_time', 'job_end_time', 'start_time', 'end_time',
                       'settings', 'put_settings', 'template', 'put_template', 'alias_actions', 'change_aliases']:
                job_history[key] = self.__dict__[key]

        self.client.index(DATAXES_INDEX, DATAXES_TYPE, job_history, self.dataxes_index_name())

    def create_dataxes_index_if_not_exists(self):
        if not self.client.indices.exists(DATAXES_INDEX):
            self.client.indices.create(DATAXES_INDEX, {
                "settings": {
                    "index": {
                        "number_of_shards": "1",
                        "number_of_replicas": "0",
                        "auto_expand_replicas": "0-all"
                    }
                },
                "mappings": {
                    DATAXES_TYPE: {
                        "properties": {
                            "end_time": {
                                "type": "date"
                            }
                        }
                    }
                }
            })

    def index_alias_when_incr(self):
        index_alias = {}
        alias = self.dataxes_alias_name()
        if alias:
            response = self.client.cat.aliases(alias, h='i')
            current_indices = response.splitlines()
            for index_name in current_indices:
                ins = index_name.split('@')
                if 2 <= len(ins) <= 3 and ins[0] == alias:
                    index_alias[index_name] = self.dataxes_index_name(ins[1])
        return index_alias

    def _es_change_aliases(self, alias_actions):
        if alias_actions:
            self.client.indices.update_aliases({"actions": alias_actions})

    def _es_get_index_aliases(self, name):
        response = self.client.indices.get_alias(name)
        return response[name]['aliases'].keys()

    def _es_get_candidate_indices(self, new=True):
        response = self.client.cat.aliases(".{}@{}".format(self.dataxes_alias_name(), "new" if new else "old"), h='i')
        candidate_indices = []
        for candidate_index in response.splitlines():
            if self.dataxes_partition_name():
                if len(candidate_index.split('@')) == 3 and candidate_index.split('@')[1] == self.dataxes_partition_name():
                    candidate_indices.append(candidate_index)
            else:
                candidate_indices.append(candidate_index)
        return candidate_indices

    def _es_get_current_indices(self):
        if self.dataxes_partition_name():
            response = self.client.cat.aliases(self.dataxes_partition_alias_name(), h='i')
        else:
            response = self.client.cat.aliases(self.dataxes_alias_name(), h='i')
        return response.splitlines()

    def _es_delete_new_candidate_indices(self):
        for index in self._es_get_candidate_indices():
            logging.info("作业执行前发现待切换索引{}，删除！".format(index))
            self.client.indices.delete(index)

    def dataxes_alias_change(self, new=True):
        """
        索引名约定规则: {$index_name}(@{$partition})@{$version}
        $index_name: 索引名，会作为别名指向
        $partition: 分区名，约定的概念，可有可无，用于扩充同一索引
        $version: yyyyMMddhhmmss的时间，为作业的end_time
        """
        alias = self.dataxes_alias_name()

        candidate_indices = self._es_get_candidate_indices(new)
        current_indices = self._es_get_current_indices()
        backup_indices = self._es_get_candidate_indices(not new)
        assert new or not backup_indices, "回滚时不应该有待切换的新索引: {}".format(','.join(backup_indices))

        actions_ = []
        relations_ = {}

        if candidate_indices:
            for index in candidate_indices:
                index_partition = '@'.join(index.split('@')[:-1])
                assert not relations_.get(index_partition), "待切换索引(分区){}有多个".format(index_partition)
                relations_[index_partition] = [index]
                old_aliases = self._es_get_index_aliases(index)
                for old_alias in old_aliases:
                    actions_.append({"remove": {"index": index, "alias": old_alias}})
                actions_.append({"add": {"index": index, "alias": alias}})
                if len(index.split('@')) == 3:
                    partition = index.split('@')[1]
                    actions_.append({"add": {"index": index, "alias": '{}@{}'.format(alias, partition)}})

            # remove current
            for index in current_indices:
                index_partition = '@'.join(index.split('@')[:-1])
                old_aliases = self._es_get_index_aliases(index)
                relations_[index_partition] = relations_.get(index_partition, [""])
                relations_[index_partition].append(index)
                for old_alias in old_aliases:
                    actions_.append({"remove": {"index": index, "alias": old_alias}})
                actions_.append({"add": {"index": index, "alias": ".{}@{}".format(alias, "old" if new else "new")}})
            for relation_ in relations_.values():
                if len(relation_) == 1:
                    relation_.append("")

            if backup_indices:
                for index in backup_indices:
                    index_partition = '@'.join(index.split('@')[:-1])
                    relations_[index_partition] = relations_.get(index_partition, ["", ""])
                    relations_[index_partition].append(index)
                for relation_ in relations_.values():
                    if len(relation_) == 2:
                        relation_.append("")

            # print change table
            if not new:
                x = PrettyTable(["index / partition", "old -> current", "current -> new"])
            elif backup_indices:
                x = PrettyTable(["index / partition", "new -> current", "current -> old", "old -> delete"])
            else:
                x = PrettyTable(["index / partition", "new -> current", "current -> old"])
            x.padding_width = 1  # One space between column edges and contents (default)
            for alias_, relation_ in relations_.items():
                relation_ = [alias_] + relation_
                x.add_row(relation_)
            logging.info("根据DataXes别名规则与配置，将按如下动作切换索引别名：\n{}".format(x))

            self.alias_actions = """{}""".format(x)
            self._es_change_aliases(actions_)
            if backup_indices:
                for index in backup_indices:
                    logging.info("删除旧版本的备份索引: {}".format(index))
                    self.client.indices.delete(index)
        else:
            logging.info("未发现待切换索引，不做任何切换")

    def rollback(self):
        self.dataxes_alias_change(new=False)

    def rollforward(self):
        self.dataxes_alias_change(new=True)


class JdbcReader:
    def __init__(self, reader_name, url, username, password):
        self.reader_name = reader_name
        self.url = url
        self.username = username
        self.password = password

    def reader_config_by_sqls(self, sqls):
        return {
            "name": self.reader_name,
            "parameter": {
                "username": self.username,
                "password": self.password,
                "connection": [
                    {
                        "querySql": sqls,
                        "jdbcUrl": self.url if type(self.url) is list else [self.url]
                    }
                ]
            }
        }
