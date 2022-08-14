# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import itertools as it
import warnings

from typing import List, Optional, Union

import bpy

from bpy.props import (BoolProperty,
                       CollectionProperty,
                       EnumProperty,
                       FloatProperty,
                       IntProperty,
                       PointerProperty,
                       StringProperty)

from bpy.types import PropertyGroup


from .channel import BasicChannel, Channel
from .preferences import get_addon_preferences

from .utils.layer_stack_utils import (get_layer_stack_by_id,
                                      get_layer_stack_from_prop)
from .utils.naming import unique_name_in
from .utils.nodes import (set_node_group_vector_defaults,
                          group_output_link_default)

LAYER_TYPES = (('MATERIAL_PAINT', "Material Paint",
                "An image-based layer for painting"),
               ('MATERIAL_FILL', "Material Fill", "A fill layer"))

# Set of valid Enum Strings for LAYER_TYPES
_VALID_LAYER_TYPES = set(x[0] for x in LAYER_TYPES)


class MaterialLayerRef(PropertyGroup):
    """Reference to a MaterialLayer instance. The MaterialLayer may be
    accessed using the resolve method.
    This classes __eq__ method returns True for any MaterialLayerRef that
    refers to the same layer as this ref, or for the MaterialLayer that this
    ref refers to.
    The bool value of a MaterialLayerRef is False if it does not refer to
    any layer, otherwise it is True (even if the layer refered to is invalid).
    """

    # Name is actually the layer's identifier (allows for easier
    # access from CollectionProperty)
    name: StringProperty(
        name="Identifier"
    )

    def __bool__(self):
        return bool(self.name)

    def __eq__(self, other):
        if not self.name:
            return False

        if isinstance(other, MaterialLayerRef):
            return other.name == self.name
        if isinstance(other, MaterialLayer):
            return other.identifier == self.name
        return False

    def initialize(self, layer: Optional[MaterialLayer]) -> None:
        self.set(layer)
        if self.id_data is not layer.id_data:
            raise RuntimeError("MaterialLayerRef only supported on objects "
                               "with the same id_data as 'layer'")

    @property
    def identifier(self) -> str:
        """The identifier of the layer that this ref references."""
        return self.name

    @property
    def layer_stack(self):
        layer_stack = self.id_data.pml_layer_stack
        layer_stack_id = self["_layer_stack_id"]
        if layer_stack.identifier != layer_stack_id:
            layer_stack = get_layer_stack_by_id(layer_stack_id)
        return layer_stack

    def resolve(self) -> Optional[MaterialLayer]:
        """Returns the layer that this ref references or None if the
        layer cannot be found. Raises a RuntimeError if this reference
        is empty."""
        if not self.name:
            raise RuntimeError("Cannot resolve empty MaterialLayerRef")
        return self.layer_stack.get_layer_by_id(self.identifier)

    def set(self, layer: Optional[MaterialLayer]) -> None:
        if layer is None:
            self.name = ""
            self["_layer_stack_id"] = ""
        else:
            self.name = layer.identifier
            self["_layer_stack_id"] = layer.layer_stack.identifier


class MaterialLayer(PropertyGroup):
    """A layer of a LayerStack. This class is used for both paint layers
    and fill layers. Each layer can contain multiple channels the values
    of which are set by the layer's editable internal node tree.

    The alpha of a paint layers may be painted in Texture Paint mode,
    and is multiplied by the layer's opacity and node_mask to get the
    layer's final alpha.
    A fill layer may not be painted on and its alpha is determined only
    by its opacity and node_mask.

    A paint layer stores its alpha in an image but may only use one RGB
    channel of that image if preferences.layers_share_images is True. In
    that case the image's other channels may contain the alpha of
    different layers, so the image painted on in texture paint will be
    a temporary image with a copy of the layer's alpha and the changes
    must be written back when the layer stacks active layer is changed.

    The bool value of a layer is True for any initialized layer and False
    for an uninitialized or deleted layer.
    """
    name: StringProperty(
        name="Name",
        default="Layer",
        # Using set and get prevents CollectionProperty from
        # accessing by name. So use update instead.
        update=lambda self, context: self._update_name()
    )
    identifier: StringProperty(
        name="Identifier",
        default="",
        description="A unique identifier for this layer. Should not be "
                    "changed after the layer has been initialized"
    )
    is_initialized: BoolProperty(
        name="Is Initialized",
        description="Whether this layer is initialized and can be used",
        get=lambda self: bool(self.identifier)
    )
    # enabled is currently unavailable in the UI
    enabled: BoolProperty(
        name="Enabled",
        description="When disabled this layer is hidden",
        default=True
    )
    opacity: FloatProperty(
        name="Opacity",
        description="The opacity the layer. 0 is fully transparent, 1 is "
                    "fully opaque",
        default=1.0, min=0.0, max=1.0
    )
    layer_type: EnumProperty(
        name="Type",
        items=LAYER_TYPES,
        description="The type of this layer. May be either a fill layer or a"
                    "paint layer",
        default='MATERIAL_PAINT',
        options=set()
    )
    channels: CollectionProperty(
        type=Channel,
        name="Channels",
        description="The channels that this layer contains"
    )
    # This is the channel that is currently selected in the UI
    active_channel_index: IntProperty(
        name="Active Channel Index",
        description="This layer's selected channel in the UI",
        min=0,
    )
    # The material used for the layer's preview icon. Only created when
    # needed so may be None.
    preview_material: PointerProperty(
        type=bpy.types.Material,
        name="Preview Material",
        description="The material used for this layer's preview"
    )
    node_tree: PointerProperty(
        type=bpy.types.ShaderNodeTree,
        name="Material Node Tree",
        description="The node tree of this layer's material",
    )
    node_mask: PointerProperty(
        type=bpy.types.ShaderNodeTree,
        name="Node Mask",
        description="A node group used as a mask for this layer",
        update=lambda self, context: self._rebuild_node_tree()
    )
    image: PointerProperty(
        type=bpy.types.Image,
        name="Image",
        description="Blender image in which this layer stores its alpha value"
    )
    image_channel: IntProperty(
        name="Image Channel",
        description="The channel of 'image' in which this layer's alpha value "
                    "is  stored, or -1 if this layer uses all channels",
        min=-1, max=2, default=-1
    )
    is_baked: BoolProperty(
        name="Is Baked",
        default=False,
    )
    # Parent/child layers are not currently supported
    parent: PointerProperty(
        type=MaterialLayerRef,
        name="Parent",
        description="This layer's parent layer"
    )
    children: CollectionProperty(
        type=MaterialLayerRef,
        name="Children",
    )

    # Node names for nodes of the preview material node tree
    _PREVIEW_SHADER_NODE_NAME = "preview_shader"
    _PREVIEW_MA_NODE_NAME = "ma_group"

    def __bool__(self):
        return self.is_initialized

    def __eq__(self, other):
        if isinstance(other, MaterialLayer):
            return other.identifier == self.identifier
        return super().__eq__(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def _get_unique_name(self) -> str:
        """Returns this layers name suffixed so that it is unique in
        the layer stack. If the name is already unique then it is
        returned unaltered.
        """

        layers = self.layer_stack.layers

        # Look for a different layer with the same name
        same = next((x for x in layers if (x.name == self.name
                                           and x != self
                                           and x.is_initialized)), None)

        if same is not None:
            basename = self.name

            suffix_num = it.count(1)
            while True:
                name = f"{basename}.{next(suffix_num):02}"
                if name not in layers:
                    return name
        return self.name

    def _ensure_node_tree_output(self, channel: Channel) -> None:
        """Ensure that the layer's node tree has an output for channel
        and that it is of the correct type.
        """
        # NodeSocketInterface instances
        outputs = self.node_tree.outputs

        output = outputs.get(channel.name)
        if output is not None:
            if output.type != channel.socket_type_bl_enum:
                # Convert the existing output if it has the wrong type
                output.type = channel.socket_type_bl_enum
        else:
            output = self.node_tree.outputs.new(
                                       name=channel.name,
                                       type=channel.socket_type_bl_idname)

        if channel.socket_type == 'VECTOR':
            output.hide_value = True
        elif channel.socket_type == 'FLOAT_FACTOR':
            output.min_value = 0.0
            output.max_value = 1.0

        # Set the output's default_value
        default_value = self.layer_stack.get_channel_default_value(channel)
        if default_value is not None:
            output.default_value = default_value

        # Links any normal or tangent sockets on the Group Output node
        # so they have the expected value. Does nothing for other sockets.
        group_output_link_default(output)

    @property
    def _node_tree_name(self) -> str:
        """What the name of this layer's node tree should be when first
        created.
        """
        return f".{self.name}"

    def _preview_material_name(self) -> str:
        return f".{self.layer_stack.material.name}.{self.name}.preview"

    def add_channel(self, channel: Channel) -> Channel:
        if not isinstance(channel, BasicChannel):
            raise TypeError("channel must be an instance of BasicChannel")

        existing = self.channels.get(channel.name)
        if existing is not None:
            warnings.warn(f"Channel with name {channel.name} already exists "
                          f"in layer {self.name}")
            return existing

        added = self.channels.add()
        added.init_from_channel(channel, layer=self)

        self._ensure_node_tree_output(channel)

        self._refresh_preview_material()

        bpy.msgbus.publish_rna(key=self.channels)

        return added

    def remove_channel(self, channel_name: Union[str, BasicChannel]) -> None:
        if isinstance(channel_name, BasicChannel):
            channel_name = channel_name.name
        elif not isinstance(channel_name, str):
            raise TypeError("Expected channel name to be a Channel or a str.")

        channel = self.channels.get(channel_name)
        if channel is None:
            raise ValueError(f"Channel {channel_name} not found in "
                             f"layer {self.name}")

        ch_idx = self.channels.find(channel_name)
        assert ch_idx >= 0

        channel.delete()
        self.channels.remove(ch_idx)

        outputs = self.node_tree.outputs
        if channel_name in outputs:
            outputs.remove(outputs[channel_name])

        active_index = self.active_channel_index

        if ch_idx < active_index:
            active_index -= 1

        active_index = max(min(active_index, len(self.channels) - 1), 0)
        self.active_channel_index = active_index

        bpy.msgbus.publish_rna(key=self.channels)

    def clear_channels(self) -> None:
        for ch_name in [x.name for x in self.channels]:
            self.remove_channel(ch_name)

    def initialize(self, name, layer_stack, layer_type='MATERIAL_PAINT',
                   channels=None, enabled_channels_only=True):
        """Initializes the layer. Must be called before the layer can
        be used.
        """

        if self.layer_stack is None:
            raise RuntimeError("MaterialLayer instance must belong to an "
                               "id_data with a LayerStack instance")

        if self.is_initialized:
            raise RuntimeError(f"{self!r} is already initialized")

        if layer_type not in _VALID_LAYER_TYPES:
            raise ValueError("Expected layer_type to be a value in "
                             f"{_VALID_LAYER_TYPES}.")

        prefs = get_addon_preferences()

        self.identifier = unique_name_in(layer_stack.layers, 4,
                                         attr="identifier")

        self.name = name
        self.layer_type = layer_type
        self.enabled = True
        self.opacity = 1.0
        self.active_channel_index = 0

        self.node_tree = bpy.data.node_groups.new(type='ShaderNodeTree',
                                                  name=self._node_tree_name)

        if channels is not None:
            for ch in channels:
                if ch.enabled or not enabled_channels_only:
                    self.add_channel(ch)

        output_node = self.node_tree.nodes.new("NodeGroupOutput")
        output_node.name = "layer_output"
        output_node.label = "Layer Output"
        set_node_group_vector_defaults(self.node_tree)

        if self.layer_type == 'MATERIAL_PAINT':
            layer_stack.image_manager.allocate_image_to_layer(self)

        if self.node_tree is not None and prefs.show_previews:
            self._create_preview_material()

        assert self.is_initialized

    def delete(self) -> None:
        """Delete this layer and all of its children. The layer can
        then be reused by calling the initialize method.
        """

        if not self.is_initialized:
            return

        self.identifier = ""
        self.parent.set(None)
        self.free_bake()

        if self.has_image:
            self.layer_stack.image_manager.deallocate_layer_image(self)

        for child in self.children:
            child.resolve().delete()
        self.children.clear()

        for channel in self.channels:
            channel.delete()
        self.channels.clear()

        if self.node_tree is not None:
            bpy.data.node_groups.remove(self.node_tree)
        self.node_mask = None

        self._delete_preview_material()
        self.name = ""

        assert not self.is_initialized
        assert self.image is None
        assert self.image_channel == -1

    def convert_to(self, layer_type: str) -> None:
        if layer_type not in _VALID_LAYER_TYPES:
            raise ValueError(f"Expected a value in {_VALID_LAYER_TYPES}")

        if layer_type == self.layer_type:
            return

        layer_stack = self.layer_stack

        if layer_type == 'MATERIAL_PAINT' and self.image is None:
            layer_stack.image_manager.allocate_image_to_layer(self)

        self.layer_type = layer_type

    @property
    def descendents(self) -> List[MaterialLayer]:
        """All of the descendents of this layer ordered as in the ui
        with the youngest generations first.

        e.g.

        -child_1
            -grandchild_1
        -child_2
            -grandchild_2
            -grandchild_3
        become:
           [grandchild_3, grandchild_2, child_2, grandchild_1, child_1]
        """
        descendents = []

        for child_layer_ref in self.children:
            child_layer = child_layer_ref.resolve()
            if child_layer.children:
                descendents += child_layer.descendents
            descendents.append(child_layer)
        return descendents

    def free_bake(self) -> None:
        """Frees any baked channel of this layer. Since this method
        does not update the layer stack's node tree
        node_manager.rebuild_node_tree should be called afterwards.

        This method may be called even if the layer has no baked
        channels.
        """
        for ch in self.channels:
            if ch.is_baked:
                ch.free_bake()
        self.is_baked = False
        assert not self.any_channel_baked

    def get_layer_above(self) -> Optional[MaterialLayer]:
        if not self.is_initialized:
            raise RuntimeError("Layer is uninitialized")

        if not self.parent:
            return self.layer_stack.get_layer_above(self)

        # All initialized siblings
        siblings = [x for x in self.parent.resolve().children() if x]
        idx = siblings.index(self)

        # Return None if this layer is at the top of siblings
        return None if idx+1 == len(siblings) else siblings[idx+1].resolve()

    def get_layer_below(self) -> Optional[MaterialLayer]:
        if not self.is_initialized:
            raise RuntimeError("Layer is uninitialized")

        if not self.parent:
            return self.layer_stack.get_layer_below(self)

        # All initialized siblings
        siblings = [x for x in self.parent.resolve().children() if x]
        idx = siblings.index(self)

        # Return None if this layer is at the bottom of siblings
        return None if idx-1 == 0 else siblings[idx-1].resolve()

    def get_top_level_layer(self) -> MaterialLayer:
        """Returns the topmost ancestor of this layer or the layer
        itself if this layer has no parent.
        """
        if not self.parent:
            return self

        layer = self.parent.resolve()
        for _ in range(100):
            if not layer.parent:
                return layer
            layer = layer.parent.resolve()
        raise RuntimeError(f"Could not find top level layer for {self!r}")

    def is_descendent_of(self, other: MaterialLayer) -> bool:
        """Returns True if this layer if a descendent of other."""

        layer = self
        for _ in range(100):
            if not layer.parent:
                return False
            if layer.parent == other:
                return True
            layer = layer.parent.resolve()
        raise RuntimeError("Maximum layer recursion depth reached.")

    def replace_node_tree(self, node_tree, update_channels=False):
        """Replaces this layers internal node tree.
        Params:
            update_channels: add/remove channels from the layer to
                match the new node tree's outputs.
        """
        if self.node_tree is not None and self.node_tree is not node_tree:
            bpy.data.node_groups.remove(self.node_tree)
            self.node_tree = None

        self.node_tree = node_tree

        if node_tree is None:
            return

        node_tree.name = self._node_tree_name

        layer_stack_chs = self.layer_stack.channels

        if update_channels:
            node_output_names = {x.name for x in node_tree.outputs}

            if self.is_base_layer:
                # For the base layer want to have all the layer stack's
                # enabled channels
                node_output_names.update([ch.name for ch in layer_stack_chs
                                          if ch.enabled])

            # Add any channels that are on the node tree but not on the
            # layer
            for ch_name in node_output_names:
                ch = self.channels.get(ch_name)

                if ch is None and ch_name in layer_stack_chs:
                    self.add_channel(layer_stack_chs[ch_name])

            # Remove any channels not found on the node tree
            for ch in reversed(list(self.channels)):
                if ch.name not in node_output_names:
                    self.remove_channel(ch)

        # Ensure the node tree has all the channels of this layer and
        # that they're the correct type
        for ch in self.channels:
            self._ensure_node_tree_output(ch)

            # Only channels enabled in the layer stack should be enabled
            # on the layer
            if ch.name in layer_stack_chs:
                ch.enabled = layer_stack_chs[ch.name].enabled

        # Add nodes so that any unlinked normal or tangent output the
        # correct default value rather than just a constant vector.
        set_node_group_vector_defaults(node_tree)

        self._refresh_preview_material()

    def _create_preview_material(self) -> None:
        if self.node_tree is None:
            return

        if self.preview_material is None:
            self.preview_material = bpy.data.materials.new(
                name=self._preview_material_name())

        ma = self.preview_material
        ma.use_nodes = True

        layer_stack = self.layer_stack
        stack_channels = layer_stack.channels

        nodes = ma.node_tree.nodes
        links = ma.node_tree.links
        nodes.clear()

        group_node = nodes.new("ShaderNodeGroup")
        group_node.name = self._PREVIEW_MA_NODE_NAME
        group_node.node_tree = self.node_tree

        if layer_stack.group_to_connect is None:
            shader = nodes.new("ShaderNodeBsdfPrincipled")
        else:
            shader = nodes.new("ShaderNodeGroup")
            shader.node_tree = layer_stack.group_to_connect

        shader.name = self._PREVIEW_SHADER_NODE_NAME
        shader.location.x += 300

        for output in group_node.outputs:
            shader_input = shader.inputs.get(output.name)
            if (shader_input is not None
                    and output.name in stack_channels
                    and stack_channels[output.name].enabled):
                links.new(shader_input, output)

        ma_out = nodes.new("ShaderNodeOutputMaterial")
        ma_out.location.x += 600
        links.new(ma_out.inputs[0], shader.outputs[0])

        ma.preview_ensure()

    def _refresh_preview_material(self):
        if self.preview_material is None:
            return

        node_tree = self.preview_material.node_tree

        try:
            group_node = node_tree.nodes[self._PREVIEW_MA_NODE_NAME]
            shader = node_tree.nodes[self._PREVIEW_SHADER_NODE_NAME]
        except KeyError as e:
            warnings.warn(f"Error refreshing preview material: {str(e)}\b"
                          "Recreating material node tree.")
            self._create_preview_material()
            return

        group_node.node_tree = self.node_tree

        for output in group_node.outputs:
            shader_input = shader.inputs.get(output.name)

            if shader_input is not None:
                node_tree.links.new(shader_input, output)

    def _delete_preview_material(self):
        if self.preview_material is not None:
            bpy.data.materials.remove(self.preview_material)

    def _rebuild_node_tree(self):
        if not self.is_initialized:
            return
        layer_stack = self.layer_stack
        if layer_stack.is_initialized:
            self.layer_stack.node_manager.rebuild_node_tree()

    def _update_name(self) -> None:
        unique_name = self._get_unique_name()
        if unique_name != self.name:
            assert unique_name not in self.layer_stack.layers
            self.name = unique_name
        else:
            if self.node_tree is not None:
                self.node_tree.name = self._node_tree_name

    @property
    def active_channel(self) -> Optional[Channel]:
        """The channel that is currently selected in the UI."""
        if not self.channels:
            return None
        return self.channels[self.active_channel_index]

    @active_channel.setter
    def active_channel(self, channel: Channel) -> None:
        ch_idx = self.channels.find(channel.name)
        if ch_idx < 0:
            raise ValueError(f"Channel {channel.name} not found in "
                             f"layer {self.name}")
        self.active_channel_index = ch_idx

    @property
    def any_channel_baked(self) -> bool:
        """Returns True if any of this layer's channels is baked."""
        return any(x.is_baked for x in self.channels)

    @property
    def has_image(self) -> bool:
        return self.image is not None

    @property
    def is_base_layer(self) -> bool:
        """Same as layer == layer.layer_stack.base_layer"""
        return self == self.layer_stack.base_layer

    @property
    def is_top_level(self) -> bool:
        """Whether this layer is at top level of the layer stack
        i.e. has a stack_depth of 0 (no parent layer).
        """
        return not self.parent and self.is_initialized

    @property
    def layer_stack(self):
        return get_layer_stack_from_prop(self)

    @property
    def preview_icon(self) -> int:
        show_previews = get_addon_preferences().show_previews

        if self.preview_material is None:
            if (show_previews
                and self.node_tree is not None
                and not bpy.app.timers.is_registered(
                                        self._create_preview_material)):
                # Can't create the preview material in certain contexts
                # e.g. from UIList.draw_item. So set a timer instead.
                bpy.app.timers.register(self._create_preview_material)
            return 0
        if self.preview_material.preview is None:
            return 0

        if not show_previews:
            self._delete_preview_material()
            return 0

        return self.preview_material.preview.icon_id

    @property
    def stack_depth(self) -> int:
        """The depth of this layer in its layer stack. A layer with a
        depth of 0 is a topmost layer with no parent, a layer with a
        depth of 2 has a parent and a grandparent etc."""

        # Handle most common cases in if statements
        if not self.parent:
            return 0
        parent = self.parent.resolve()
        if not parent.parent:
            return 1

        # May raises a RecursionError in the case of cyclic ancestors
        # (which shouldn't happen)
        return parent.parent.resolve().stack_depth + 2

    @property
    def uses_shared_image(self):
        return self.image is not None and self.image_channel >= 0


classes = (MaterialLayerRef, MaterialLayer)

register, unregister = bpy.utils.register_classes_factory(classes)
