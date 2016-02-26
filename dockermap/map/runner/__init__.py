# -*- coding: utf-8 -*-
from collections import namedtuple

from six import with_metaclass

from ..action import ACTION_CREATE, ACTION_REMOVE
from ..policy import PolicyUtilMeta, PolicyUtil


ActionConfig = namedtuple('ActionConfig', ['map_name', 'container_map', 'config_name', 'container_config',
                                           'client_name', 'client_config', 'instance_name'])


class RunnerMeta(PolicyUtilMeta):
    def __init__(cls, name, bases, dct):
        cls._a_methods = attached_methods = []
        cls._i_methods = instance_methods = []
        for base in bases:
            if hasattr(base, 'attached_action_method_names'):
                attached_methods.extend((a_type_name, a_method_name)
                                        for a_type_name, a_method_name in base.attached_action_method_names)
            if hasattr(base, 'instance_action_method_names'):
                instance_methods.extend((a_type_name, a_method_name)
                                        for a_type_name, a_method_name in base.instance_action_method_names)
        a_method_names = dct.get('attached_action_method_names')
        if a_method_names:
            attached_methods.extend((a_type_name, a_method_name)
                                    for a_type_name, a_method_name in a_method_names)
        i_method_names = dct.get('instance_action_method_names')
        if i_method_names:
            instance_methods.extend((a_type_name, a_method_name)
                                    for a_type_name, a_method_name in i_method_names)
        super(RunnerMeta, cls).__init__(name, bases, dct)


class AbstractRunner(with_metaclass(RunnerMeta, PolicyUtil)):
    def __new__(cls, *args, **kwargs):
        instance = super(AbstractRunner, cls).__new__(cls, *args, **kwargs)
        instance.attached_methods = {
            action_name: getattr(instance, action_method)
            for action_name, action_method in cls._a_methods
        }
        instance.instance_methods = {
            action_name: getattr(instance, action_method)
            for action_name, action_method in cls._i_methods
        }
        return instance

    def get_client_action_config(self, action):
        """

        :param action:
        :type action: dockermap.map.action.InstanceAction
        :return:
        :rtype: (docker.Client, ActionConfig)
        """
        client_config = self._policy.clients[action.client_name]
        client = client_config.get_client()
        c_map = self._policy.container_maps[action.map_name]
        c_config = c_map.get_existing(action.config_name)
        return client, ActionConfig(action.map_name, c_map, action.config_name, c_config, action.client_name,
                                    client_config, action.instance_name)

    def run_actions(self, attached_actions, instance_actions):
        """

        :param attached_actions:
        :type attached_actions: list[dockermap.map.action.InstanceAction]
        :param instance_actions:
        :type instance_actions: list[dockermap.map.action.InstanceAction]
        :return:
        :rtype: __generator[dict]
        """
        aname = self._policy.aname
        for action in attached_actions:
            client, action_config = self.get_client_action_config(action)
            a_parent_name = action.config_name if action_config.container_map.use_attached_parent_name else None
            container_name = aname(action.map_name, action.instance_name, parent_name=a_parent_name)
            for action_type in action.action_types:
                a_method = self.attached_methods.get(action_type)
                if not a_method:
                    raise ValueError("Invalid action for attached containers.", action_type)
                res = a_method(client, action_config, container_name, **action.extra_data)
                if action_type == ACTION_CREATE:
                    self._policy.container_names[action.client_name].add(container_name)
                elif action_type == ACTION_REMOVE:
                    self._policy.container_names[action.client_name].discard(container_name)
                if res is not None:
                    yield res

        cname = self._policy.cname
        for action in instance_actions:
            client, action_config = self.get_client_action_config(action)
            container_name = cname(action.map_name, action.config_name, action.instance_name)
            for action_type in action.action_types:
                c_method = self.instance_methods.get(action_type)
                if not c_method:
                    raise ValueError("Invalid action for instance containers.", action_type)
                res = c_method(client, action_config, container_name, **action.extra_data)
                if action_type == ACTION_CREATE:
                    self._policy.container_names[action.client_name].add(container_name)
                elif action_type == ACTION_REMOVE:
                    self._policy.container_names[action.client_name].discard(container_name)
                if res is not None:
                    yield res
