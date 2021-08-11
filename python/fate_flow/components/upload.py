#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import os
import shutil
import time
import uuid

from fate_arch import storage
from fate_arch.common import EngineType, file_utils, log, path_utils
from fate_arch.session import Session
from fate_arch.storage import DEFAULT_ID_DELIMITER, EggRollStoreType, StorageEngine
from fate_flow.components._base import BaseParam, ComponentBase, ComponentMeta
from fate_flow.entity.metric import Metric, MetricMeta
from fate_flow.manager.data_manager import DataTableTracker
from fate_flow.scheduling_apps.client import ControllerClient
from fate_flow.utils import data_utils, job_utils

LOGGER = log.getLogger()

upload_cpn_meta = ComponentMeta("Upload")


@upload_cpn_meta.impl_param
class UploadParam(BaseParam):
    def __init__(
        self,
        file="",
        head=1,
        id_delimiter=DEFAULT_ID_DELIMITER,
        partition=10,
        namespace="",
        name="",
        storage_engine="",
        storage_address=None,
        destroy=False,
        extend_sid=False,
        auto_increasing_sid=False,
    ):
        self.file = file
        self.head = head
        self.id_delimiter = id_delimiter
        self.partition = partition
        self.namespace = namespace
        self.name = name
        self.storage_engine = storage_engine
        self.storage_address = storage_address
        self.destroy = destroy
        self.extend_sid = extend_sid
        self.auto_increasing_sid = auto_increasing_sid

    def check(self):
        return True


@upload_cpn_meta.impl_runner("local")
class Upload(ComponentBase):
    def __init__(self):
        super(Upload, self).__init__()
        self.MAX_PARTITIONS = 1024
        self.MAX_BYTES = 1024 * 1024 * 8 * 500
        self.parameters = {}
        self.table = None

    def run(self, component_parameters=None, args=None):
        self.parameters = component_parameters["ComponentParam"]
        LOGGER.info(self.parameters)
        LOGGER.info(args)
        self.parameters["role"] = component_parameters["role"]
        self.parameters["local"] = component_parameters["local"]
        storage_engine = self.parameters["storage_engine"]
        storage_address = self.parameters["storage_address"]
        # if not set storage, use job storage as default
        if not storage_engine:
            storage_engine = args["job_parameters"].storage_engine
        if not storage_address:
            storage_address = args["job_parameters"].engines_address[EngineType.STORAGE]
        job_id = self.task_version_id.split("_")[0]
        if not os.path.isabs(self.parameters.get("file", "")):
            self.parameters["file"] = os.path.join(
                file_utils.get_project_base_directory(), self.parameters["file"]
            )
        if not os.path.exists(self.parameters["file"]):
            raise Exception(
                "%s is not exist, please check the configure"
                % (self.parameters["file"])
            )
        if not os.path.getsize(self.parameters["file"]):
            raise Exception("%s is an empty file" % (self.parameters["file"]))
        name, namespace = self.parameters.get("name"), self.parameters.get("namespace")
        _namespace, _table_name = self.generate_table_name(self.parameters["file"])
        if namespace is None:
            namespace = _namespace
        if name is None:
            name = _table_name
        read_head = self.parameters["head"]
        if read_head == 0:
            head = False
        elif read_head == 1:
            head = True
        else:
            raise Exception("'head' in conf.json should be 0 or 1")
        partitions = self.parameters["partition"]
        if partitions <= 0 or partitions >= self.MAX_PARTITIONS:
            raise Exception(
                "Error number of partition, it should between %d and %d"
                % (0, self.MAX_PARTITIONS)
            )
        sess = Session(
            session_id=job_utils.generate_session_id(
                self.tracker.task_id,
                self.tracker.task_version,
                self.tracker.role,
                self.tracker.party_id,
            )
        )
        if self.parameters.get("destroy", False):
            storage_session = sess.storage(namespace=namespace, name=name)
            if storage_session:
                table = storage_session.get_table()
                if table:
                    LOGGER.info(
                        f"destroy table name: {name} namespace: {namespace} engine: {table.get_engine()}"
                    )
                    table.destroy()
                else:
                    LOGGER.info(
                        f"can not found table name: {name} namespace: {namespace}, pass destroy"
                    )
            else:
                LOGGER.info(
                    f"can not found table name: {name} namespace: {namespace}, pass destroy"
                )
        address_dict = storage_address.copy()
        storage_session = sess.storage(
            storage_engine=storage_engine, options=self.parameters.get("options")
        )
        upload_address = {}
        if storage_engine in {StorageEngine.EGGROLL, StorageEngine.STANDALONE}:
            upload_address = {
                "name": name,
                "namespace": namespace,
                "storage_type": EggRollStoreType.ROLLPAIR_LMDB,
            }
        elif storage_engine in {StorageEngine.MYSQL}:
            if not address_dict.get("db") or not address_dict.get("name"):
                upload_address = {"db": namespace, "name": name}
        elif storage_engine in {StorageEngine.PATH}:
            upload_address = {"path": self.parameters["file"]}
        elif storage_engine in {StorageEngine.HDFS}:
            upload_address = {
                "path": data_utils.default_input_fs_path(
                    name=name,
                    namespace=namespace,
                    prefix=address_dict.get("path_prefix"),
                )
            }
        else:
            raise RuntimeError(f"can not support this storage engine: {storage_engine}")
        address_dict.update(upload_address)
        LOGGER.info(f"upload to {storage_engine} storage, address: {address_dict}")
        address = storage.StorageTableMeta.create_address(
            storage_engine=storage_engine, address_dict=address_dict
        )
        self.parameters["partitions"] = partitions
        self.parameters["name"] = name
        self.table = storage_session.create_table(address=address, **self.parameters)
        data_table_count = None
        if storage_engine not in [StorageEngine.PATH]:
            data_table_count = self.save_data_table(job_id, name, namespace, head)
        else:
            data_table_count = self.get_data_table_count(
                self.parameters["file"], name, namespace
            )
        self.table.meta.update_metas(in_serialized=True)
        DataTableTracker.create_table_tracker(
            table_name=name,
            table_namespace=namespace,
            entity_info={"job_id": job_id, "have_parent": False},
        )
        LOGGER.info("------------load data finish!-----------------")
        # rm tmp file
        try:
            if "{}/fate_upload_tmp".format(job_id) in self.parameters["file"]:
                LOGGER.info("remove tmp upload file")
                LOGGER.info(os.path.dirname(self.parameters["file"]))
                shutil.rmtree(os.path.dirname(self.parameters["file"]))
        except:
            LOGGER.info("remove tmp file failed")
        LOGGER.info("file: {}".format(self.parameters["file"]))
        LOGGER.info("total data_count: {}".format(data_table_count))
        LOGGER.info("table name: {}, table namespace: {}".format(name, namespace))

    def save_data_table(self, job_id, dst_table_name, dst_table_namespace, head=True):
        input_file = self.parameters["file"]
        input_feature_count = self.get_count(input_file)
        with open(input_file, "r") as fin:
            lines_count = 0
            if head is True:
                data_head = fin.readline()
                input_feature_count -= 1
                self.table.meta.update_metas(
                    schema=data_utils.get_header_schema(
                        header_line=data_head,
                        id_delimiter=self.parameters["id_delimiter"],
                    )
                )
                self.table.meta.update_metas(
                    schema=data_utils.get_header_schema(
                        header_line=data_head,
                        id_delimiter=self.parameters["id_delimiter"],
                        extend_sid=self.parameters["extend_sid"],
                    ),
                    auto_increasing_sid=self.parameters["auto_increasing_sid"],
                    extend_sid=self.parameters["extend_sid"],
                )
            n = 0
            fate_uuid = uuid.uuid1().hex
            if not self.parameters["extend_sid"]:
                get_line = data_utils.get_data_line
            elif not self.parameters["auto_increasing_sid"]:
                get_line = data_utils.get_sid_data_line
            else:
                get_line = data_utils.get_auto_increasing_sid_data_line
            while True:
                data = list()
                lines = fin.readlines(self.MAX_BYTES)
                line_index = 0
                if lines:
                    # self.append_data_line(lines, data, n)
                    for line in lines:
                        values = line.rstrip().split(self.parameters["id_delimiter"])
                        k, v = get_line(
                            values=values,
                            line_index=line_index,
                            extend_sid=self.parameters["extend_sid"],
                            auto_increasing_sid=self.parameters["auto_increasing_sid"],
                            id_delimiter=self.parameters["id_delimiter"],
                            fate_uuid=fate_uuid,
                        )
                        data.append((k, v))
                        line_index += 1
                    lines_count += len(data)
                    save_progress = lines_count / input_feature_count * 100 // 1
                    job_info = {
                        "progress": save_progress,
                        "job_id": job_id,
                        "role": self.parameters["local"]["role"],
                        "party_id": self.parameters["local"]["party_id"],
                    }
                    ControllerClient.update_job(job_info=job_info)
                    self.table.put_all(data)
                    if n == 0:
                        self.table.meta.update_metas(part_of_data=data)
                else:
                    table_count = self.table.count()
                    self.table.meta.update_metas(
                        count=table_count, partitions=self.parameters["partition"]
                    )
                    self.table.meta.update_metas(
                        count=table_count,
                        partitions=self.parameters["partition"],
                        extend_sid=self.parameters["extend_sid"],
                    )
                    self.save_meta(
                        dst_table_namespace=dst_table_namespace,
                        dst_table_name=dst_table_name,
                        table_count=table_count,
                    )
                    return table_count
                n += 1

    def get_count(self, input_file):
        with open(input_file, "r", encoding="utf-8") as fp:
            count = 0
            for line in fp:
                count += 1
        return count

    def generate_table_name(self, input_file_path):
        str_time = time.strftime("%Y%m%d%H%M%S", time.localtime())
        file_name = input_file_path.split(".")[0]
        file_name = file_name.split("/")[-1]
        return file_name, str_time

    def save_meta(self, dst_table_namespace, dst_table_name, table_count):
        self.tracker.log_output_data_info(
            data_name="upload",
            table_namespace=dst_table_namespace,
            table_name=dst_table_name,
        )

        self.tracker.log_metric_data(
            metric_namespace="upload",
            metric_name="data_access",
            metrics=[Metric("count", table_count)],
        )
        self.tracker.set_metric_meta(
            metric_namespace="upload",
            metric_name="data_access",
            metric_meta=MetricMeta(name="upload", metric_type="UPLOAD"),
        )

    def get_data_table_count(self, path, name, namespace):
        count = path_utils.get_data_table_count(path)
        self.save_meta(
            dst_table_namespace=namespace, dst_table_name=name, table_count=count
        )
        self.table.meta.update_metas(count=count)
        return count
