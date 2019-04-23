"""
A collection of property estimator compute backends which use dask as the distribution engine.
"""
import importlib
import logging
import multiprocessing
import os
import shutil

from dask import distributed
from dask_jobqueue import LSFCluster
from distributed import get_worker, Adaptive
from distributed.utils import ignoring
from propertyestimator.workflow.plugins import available_protocols
from simtk import unit

from .backends import PropertyEstimatorBackend, ComputeResources, QueueWorkerResources

# _logger = logging.getLogger(__name__)


class _ImprovedAdaptive(Adaptive):
    """A temporary extension to the dask distributed `Adaptive` object which
    implements the changes proposed in the below dask distributed issue:

    https://github.com/dask/distributed/issues/2329

    Warnings
    --------
    When the PR which implements these changes gets merged into a release,
    this class should be removed.
    """

    def needs_cpu(self):
        """
        Check if the cluster is CPU constrained (too many tasks per core)
        Notes
        -----
        Returns ``True`` if the occupancy per core is some factor larger
        than ``startup_cost`` and the number of tasks exceeds the number of
        cores
        """
        total_occupancy = self.scheduler.total_occupancy
        total_cores = self.scheduler.total_ncores

        if total_occupancy / (total_cores + 1e-9) > self.startup_cost * 2:

            # _logger.info("CPU limit exceeded [%d occupancy / %d cores]",
            #              total_occupancy, total_cores)

            tasks_processing = 0

            for w in self.scheduler.workers.values():
                tasks_processing += len(w.processing)

                if tasks_processing > total_cores:

                    # _logger.info(
                    #     "pending tasks exceed number of cores "
                    #     "[%d tasks / %d cores]",
                    #     tasks_processing, total_cores)

                    return True

        return False


class _AdaptiveLSFCLuster(LSFCluster):
    """A temporary extension to the dask job-queue `LSFCluster` object which
    implements the changes proposed in the below dask distributed issue:

    https://github.com/dask/distributed/issues/2329

    Warnings
    --------
    When the PR which implements these changes gets merged into a release,
    this class should be removed.
    """

    def adapt(self, **kwargs):

        with ignoring(AttributeError):
            self._adaptive.stop()

        if not hasattr(self, '_adaptive_options'):
            self._adaptive_options = {}

        self._adaptive_options.update(kwargs)
        self._adaptive = _ImprovedAdaptive(self.scheduler, self, **self._adaptive_options)
        return self._adaptive


class DaskLSFBackend(PropertyEstimatorBackend):
    """A property estimator backend which uses a dask-jobqueue `LSFCluster`
    objects to run calculations within an existing LSF queue.
    """

    def __init__(self,
                 minimum_number_of_workers=1,
                 maximum_number_of_workers=1,
                 resources_per_worker=QueueWorkerResources(),
                 default_memory_unit=unit.giga*unit.byte,
                 queue_name='default',
                 setup_script_commands=None,
                 adaptive_interval='10000ms'):

        """Constructs a new DaskLocalClusterBackend

        Parameters
        ----------
        minimum_number_of_workers: int
            The minimum number of workers to request from the queue system.
        maximum_number_of_workers: int
            The maximum number of workers to request from the queue system.
        resources_per_worker: QueueWorkerResources
            The resources to request per worker.
        default_memory_unit: simtk.Unit
            The default unit used by the LSF queuing system when
            defining memory usage limits / requirements - this
            must be compatible with `unit.bytes`.
        queue_name: str
            The name of the queue which the workers will be requested
            from.
        setup_script_commands: list of str
            A list of bash script commands to call within the queue submission
            script before the call to launch the dask worker.

            This may include activating a python environment, or loading
            an environment module
        adaptive_interval: str
            The interval between attempting to either scale up or down
            the cluster, of of the from 'XXXms'.

        Examples
        --------
        To create an LSF queueing compute backend which will attempt to spin up
        workers which have access to a single GPU.

        >>> # Create a resource object which will request a worker with
        >>> # one gpu which will stay alive for five hours.
        >>> from propertyestimator.backends import QueueWorkerResources
        >>>
        >>> resources = QueueWorkerResources(number_of_threads=1,
        >>>                                  number_of_gpus=1,
        >>>                                  preferred_gpu_toolkit=QueueWorkerResources.GPUToolkit.CUDA,
        >>>                                  wallclock_time_limit='05:00')
        >>>
        >>> # Define the set of commands which will set up the correct environment
        >>> # for each of the workers.
        >>> worker_script_commands = [
        >>>     'module load cuda/9.2',
        >>> ]
        >>>
        >>> # Create the backend which will adaptively try to spin up between one and
        >>> # ten workers with the requested resources depending on the calculation load.
        >>> from propertyestimator.backends import DaskLSFBackend
        >>> from simtk.unit import unit
        >>> lsf_backend = DaskLSFBackend(minimum_number_of_workers=1,
        >>>                              maximum_number_of_workers=10,
        >>>                              resources_per_worker=resources,
        >>>                              default_memory_unit=unit.gigabyte,
        >>>                              queue_name='gpuqueue',
        >>>                              setup_script_commands=worker_script_commands)
        """

        super().__init__(minimum_number_of_workers, resources_per_worker)

        assert isinstance(resources_per_worker, QueueWorkerResources)

        assert minimum_number_of_workers <= maximum_number_of_workers

        if resources_per_worker.number_of_gpus > 0:

            if resources_per_worker.preferred_gpu_toolkit == ComputeResources.GPUToolkit.OpenCL:
                raise ValueError('The OpenCL gpu backend is not currently supported.')

            if resources_per_worker.number_of_gpus > 1:
                raise ValueError('Only one GPU per worker is currently supported.')

        self._cluster = None
        self._client = None

        self._minimum_number_of_workers = minimum_number_of_workers
        self._maximum_number_of_workers = maximum_number_of_workers

        self._default_memory_unit = default_memory_unit

        self._queue_name = queue_name

        self._setup_script_commands = setup_script_commands

        self._adaptive_interval = adaptive_interval

    def start(self):

        requested_memory = self._resources_per_worker.per_thread_memory_limit

        memory_default_unit = requested_memory.value_in_unit(self._default_memory_unit)
        memory_bytes = requested_memory.value_in_unit(unit.byte)

        memory_string = '{}{}'.format(memory_default_unit, self._default_memory_unit.get_symbol())

        # Dask assumes we will be using mega bytes as the default unit, so we need
        # to multiply by a corrective factor to remove this assumption.
        lsf_byte_scale = (1 * (unit.mega * unit.byte)).value_in_unit(self._default_memory_unit)
        memory_bytes *= lsf_byte_scale

        job_extra = None

        if self._resources_per_worker.number_of_gpus > 0:

            job_extra = [
                '-gpu num={}:j_exclusive=yes:mode=shared:mps=no:'.format(self._resources_per_worker.number_of_gpus)
            ]

        self._cluster = _AdaptiveLSFCLuster(queue=self._queue_name,
                                            cores=self._resources_per_worker.number_of_threads,
                                            memory=memory_string,
                                            walltime=self._resources_per_worker.wallclock_time_limit,
                                            mem=memory_bytes,
                                            job_extra=job_extra,
                                            env_extra=self._setup_script_commands,
                                            local_directory='dask-worker-space')

        self._cluster.adapt(minimum=self._minimum_number_of_workers,
                            maximum=self._maximum_number_of_workers, interval=self._adaptive_interval)

        self._client = distributed.Client(self._cluster,
                                          processes=False)

    def stop(self):

        self._client.close()
        self._cluster.close()

    @staticmethod
    def _wrapped_function(function, *args, **kwargs):

        available_resources = kwargs['available_resources']

        protocols_to_import = kwargs.pop('available_protocols')
        per_worker_logging = kwargs.pop('per_worker_logging')

        gpu_assignments = kwargs.pop('gpu_assignments')

        # Each spun up worker doesn't automatically import
        # all of the modules which were imported in the main
        # launch script, and as such custom plugins will no
        # longer be registered. We re-import / register them
        # here.
        for protocol_class in protocols_to_import:

            module_name = '.'.join(protocol_class.split('.')[:-1])
            class_name = protocol_class.split('.')[-1]

            imported_module = importlib.import_module(module_name)
            available_protocols[class_name] = getattr(imported_module, class_name)

        # Set up the logging per worker if the flag is set to True.
        if per_worker_logging:

            formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s',
                                          datefmt='%H:%M:%S')

            # Each worker should have its own log file.
            logger = logging.getLogger()

            if not len(logger.handlers):

                logger_handler = logging.FileHandler('{}.log'.format(get_worker().id))
                logger_handler.setFormatter(formatter)

                logger.setLevel(logging.INFO)
                logger.addHandler(logger_handler)

        if available_resources.number_of_gpus > 0:

            worker_id = distributed.get_worker().id

            available_resources._gpu_device_indices = ('0' if worker_id not in gpu_assignments
                                                       else gpu_assignments[worker_id])

            logging.info(f'Launching a job with access to GPUs {available_resources._gpu_device_indices}')

        return function(*args, **kwargs)

    def submit_task(self, function, *args):

        protocols_to_import = [protocol_class.__module__ + '.' +
                               protocol_class.__qualname__ for protocol_class in available_protocols.values()]

        return self._client.submit(DaskLSFBackend._wrapped_function,
                                   function,
                                   *args,
                                   available_resources=self._resources_per_worker,
                                   available_protocols=protocols_to_import,
                                   gpu_assignments={},
                                   per_worker_logging=True)


class DaskLocalClusterBackend(PropertyEstimatorBackend):
    """A property estimator backend which uses a dask `LocalCluster` to
    run calculations.
    """

    def __init__(self, number_of_workers=1, resources_per_worker=ComputeResources()):
        """Constructs a new DaskLocalClusterBackend"""

        super().__init__(number_of_workers, resources_per_worker)

        self._gpu_device_indices_by_worker = {}

        maximum_threads = multiprocessing.cpu_count()
        requested_threads = number_of_workers * resources_per_worker.number_of_threads

        if requested_threads > maximum_threads:

            raise ValueError('The total number of requested threads ({})is greater than is available on the'
                             'machine ({})'.format(requested_threads, maximum_threads))

        if resources_per_worker.number_of_gpus > 0:

            if resources_per_worker.preferred_gpu_toolkit == ComputeResources.GPUToolkit.OpenCL:
                raise ValueError('The OpenCL gpu backend is not currently supported.')

            if resources_per_worker.number_of_gpus > 1:
                raise ValueError('Only one GPU per worker is currently supported.')

            visible_devices = os.environ.get('CUDA_VISIBLE_DEVICES')

            if visible_devices is None:
                raise ValueError('The CUDA_VISIBLE_DEVICES variable is empty.')

            gpu_device_indices = visible_devices.split(',')

            if len(gpu_device_indices) != number_of_workers:
                raise ValueError('The number of available GPUs {} must match '
                                 'the number of requested workers {}.')

        self._cluster = None
        self._client = None

    def start(self):

        self._cluster = distributed.LocalCluster(self._number_of_workers,
                                                 1,
                                                 processes=False)

        if self._resources_per_worker.number_of_gpus > 0:

            for index, worker in enumerate(self._cluster.workers):
                self._gpu_device_indices_by_worker[worker.id] = str(index)

        self._client = distributed.Client(self._cluster,
                                          processes=False)

    def stop(self):

        self._client.close()
        self._cluster.close()

        if os.path.isdir('dask-worker-space'):
            shutil.rmtree('dask-worker-space')

    @staticmethod
    def _wrapped_function(function, *args, **kwargs):

        available_resources = kwargs['available_resources']
        gpu_assignments = kwargs.pop('gpu_assignments')

        if available_resources.number_of_gpus > 0:

            worker_id = distributed.get_worker().id
            available_resources._gpu_device_indices = gpu_assignments[worker_id]

            logging.info('Launching a job with access to GPUs {}'.format(gpu_assignments[worker_id]))

        return function(*args, **kwargs)

    def submit_task(self, function, *args):

        return self._client.submit(DaskLocalClusterBackend._wrapped_function,
                                   function,
                                   *args,
                                   available_resources=self._resources_per_worker,
                                   gpu_assignments=self._gpu_device_indices_by_worker)
