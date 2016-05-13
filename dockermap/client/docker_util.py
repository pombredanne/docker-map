# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import sys

import six
from docker.errors import APIError
from requests import Timeout

from ..build.context import DockerContext
from ..dep import SingleDependencyResolver


log = logging.getLogger(__name__)


def is_repo_image(image):
    """
    Checks whether the given image has a name, i.e. is a repository image. This does not imply that it is
    assigned to an external repository.

    :param image: Image structure from the Docker Remote API.
    :type image: dict
    :return: ``False`` if the only image name and tag is <none>, ``True`` otherwise.
    :rtype: bool
    """
    return image['RepoTags'][0] != '<none>:<none>'


def tag_check_function(tags):
    """
    Generates a function that checks whether the given image has any of the listed tags.

    :param tags: Tags to check for.
    :type tags: list[unicode | str] | set[unicode | str]
    :return: Function that returns ``True`` if any of the given tags apply to the image, ``False`` otherwise.
    :rtype: (unicode | str) -> bool
    """
    suffixes = [':{0}'.format(t) for t in tags]

    def _check_image(image):
        return any(r_tag.endswith(s) for s in suffixes for r_tag in image['RepoTags'])

    return _check_image


class ContainerImageResolver(SingleDependencyResolver):
    """
    Finds dependencies of containers on images and images on one another, where each container depends on exactly one
    image and each image depends on one or zero images. The purpose is only to find *if* images are used - not by what -
    in order to perform a clean-up.

    :param container_images: Set of image ids currently used by containers.
    :type container_images: set[unicode | str]
    :param images: Iterable or dictionary of images in the format `(image, parent_image)`.
    :type images: iterable
    """

    def __init__(self, container_images=None, images=None):
        super(ContainerImageResolver, self).__init__(images)
        self._container_images = container_images

    def merge_dependency(self, item, resolve_parent, parent):
        """
        Checks if any containers depend on the current image id; if not, moves down the hierarchy, checking the parent
        images.

        :param item: Image id to check for dependent items.
        :type item: unicode | str
        :param resolve_parent: Function to check parent image for dependencies.
        :param parent: Parent image id.
        :return: `True` if any dependency has been found, `False` otherwise.
        :type: bool
        """
        return item in self._container_images or super(ContainerImageResolver, self).merge_dependency(item, resolve_parent, parent)


class DockerUtilityMixin(object):
    def push_log(self, info, level, *args, **kwargs):
        """
        Writes logs. To be fully implemented by subclasses.

        :param info: Log message content.
        :type info: unicode | str
        :param level: Logging level.
        :type level: int
        :param args: Positional arguments to pass to logger.
        :param kwargs: Keyword arguments to pass to logger.
        """
        log.log(level, info, *args, **kwargs)

    def build_from_context(self, ctx, tag, **kwargs):
        """
        Builds a docker image from the given docker context with a `Dockerfile` file object.

        :param ctx: An instance of :class:`~.context.DockerContext`.
        :type ctx: dockermap.build.context.DockerContext
        :param tag: New image tag.
        :type tag: unicode | str
        :param kwargs: See :meth:`docker.client.Client.build`.
        :return: New, generated image id or `None`.
        :rtype: unicode | str
        """
        return self.build(fileobj=ctx.fileobj, tag=tag, custom_context=True, encoding=ctx.stream_encoding, **kwargs)

    def build_from_file(self, dockerfile, tag, **kwargs):
        """
        Builds a docker image from the given :class:`~dockermap.build.dockerfile.DockerFile`. Use this as a shortcut to
        :meth:`build_from_context`, if no extra data is added to the context.

        :param dockerfile: An instance of :class:`~dockermap.build.dockerfile.DockerFile`.
        :type dockerfile: dockermap.build.dockerfile.DockerFile
        :param tag: New image tag.
        :type tag: unicode | str
        :param kwargs: See :meth:`docker.client.Client.build`.
        :return: New, generated image id or ``None``.
        :rtype: unicode | str
        """
        with DockerContext(dockerfile, finalize=True) as ctx:
            return self.build_from_context(ctx, tag, **kwargs)

    def cleanup_containers(self, include_initial=False, exclude=None, raise_on_error=False):
        """
        Finds all stopped containers and removes them; by default does not remove containers that have never been
        started.

        :param include_initial: Consider containers that have never been started.
        :type include_initial: bool
        :param exclude: Container names to exclude from the cleanup process.
        :type exclude: iterable
        :param raise_on_error: Forward errors raised by the client and cancel the process. By default only logs errors.
        :type raise_on_error: bool
        """
        def _stopped_containers():
            exclude_names = set(exclude or ())
            for container in self.containers(all=True):
                c_names = [name[1:] for name in container['Names'] or ()]
                c_status = container['Status']
                if (((include_initial and c_status == '') or c_status.startswith('Exited')) and
                        exclude_names.isdisjoint(c_names)):
                    c_id = container['Id']
                    yield c_id, c_names[0] if c_names else c_id

        for cid, cn in _stopped_containers():
            try:
                self.remove_container(cn)
            except APIError as e:
                if e.response.status_code != 404:
                    self.push_log("Could not remove container '%s': %s", logging.ERROR, cn, e.explanation)
                    if raise_on_error:
                        six.reraise(*sys.exc_info())

    def cleanup_images(self, remove_old=False, keep_tags=None, raise_on_error=False):
        """
        Finds all images that are neither used by any container nor another image, and removes them; by default does not
        remove repository images.

        :param remove_old: Also removes images that have repository names, but no `latest` tag.
        :type remove_old: bool
        :param keep_tags: List of tags to not remove.
        :type keep_tags: list[unicode | str]
        :param raise_on_error: Forward errors raised by the client and cancel the process. By default only logs errors.
        :type raise_on_error: bool
        """
        used_images = set(self.inspect_container(container['Id'])['Image']
                          for container in self.containers(all=True))
        image_dependencies = ((image['Id'], image['ParentId']) for image in self.images(all=True))
        resolver = ContainerImageResolver(used_images, image_dependencies)
        if remove_old:
            check_tags = {'latest'}
            if keep_tags:
                check_tags.update(keep_tags)
            tag_check = tag_check_function(check_tags)
        elif remove_old:
            tag_check = tag_check_function(['latest'])
        else:
            tag_check = is_repo_image
        unused_images = set(image['Id'] for image in self.images()
                            if not tag_check(image) and not resolver.get_dependencies(image['Id']))
        for iid in unused_images:
            try:
                self.remove_image(iid)
            except APIError as e:
                if e.response.status_code != 404:
                    self.push_log("Could not remove image '%s': %s", logging.ERROR, iid, e.explanation)
                    if raise_on_error:
                        six.reraise(*sys.exc_info())

    def remove_all_containers(self, stop_timeout=10):
        """
        First stops (if necessary) and them removes all containers present on the Docker instance.

        :param stop_timeout: Timeout to stopping each container.
        :type stop_timeout: int
        """
        containers = [(container['Id'], container['Status'].startswith('Exited'))
                      for container in self.containers(all=True)]
        for c_id, stopped in containers:
            if not stopped:
                try:
                    self.stop(c_id, timeout=stop_timeout)
                except Timeout:
                    log.warning("Container did not stop in time - sent SIGKILL.")
        for c_id, __ in containers:
            self.remove_container(c_id)

    def get_container_names(self):
        """
        Fetches names of all present containers from Docker.

        :return: All container names.
        :rtype: set
        """
        current_containers = self.containers(all=True)
        return set(c_name[1:] for c in current_containers for c_name in c['Names'])

    def get_image_tags(self):
        """
        Fetches image labels (repository / tags) from Docker.

        :return: A dictionary, with image name and tags as the key and the image id as value.
        :rtype: dict
        """
        current_images = self.images()
        tags = {tag: i['Id'] for i in current_images for tag in i['RepoTags']}
        return tags
