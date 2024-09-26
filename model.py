import torch
import torch.utils.checkpoint
from torch import nn
from transformers.activations import ACT2FN
from transformers.cache_utils import Cache
from transformers.modeling_outputs import ModelOutput
from transformers.modeling_utils import GenerationMixin


class MyLlavaOutput(ModelOutput):
    """
        logits (`torch.FloatTensor` of shape `(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.
            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        image_hidden_states (`tuple(torch.FloatTensor)`, *optional*):
            Tuple of `torch.FloatTensor` (one for the output of the image embeddings, `(batch_size, num_images,
            sequence_length, hidden_size)`.
            image_hidden_states of the model produced by the vision encoder, and optionally by the perceiver
    """

    loss = None
    logits = None
    past_key_values = None
    hidden_states = None
    attentions = None
    image_hidden_states = None


class MyLlavaProjector(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.linear_1 = nn.Linear(cfg.image_encoder_hidden_size, cfg.llm_hidden_size, bias=True)
        self.act = ACT2FN[cfg.projector_hidden_act]
        self.linear_2 = nn.Linear(cfg.llm_hidden_size, cfg.llm_hidden_size, bias=True)

    def forward(self, image_features):
        hidden_states = self.linear_1(image_features)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states

LLAVA_INPUTS_DOCSTRING = r"""
        vision_feature_layer (`int`, *optional*, defaults to -2):
            The index of the layer to select the vision feature.
        vision_feature_select_strategy (`str`, *optional*, defaults to `"default"`):
            The feature selection strategy used to select the vision feature from the vision backbone.
            Can be one of `"default"` or `"full"`.
"""


class MyLlava(nn.Module, GenerationMixin):
    def __init__(self, cfg, image_encoder, llm, projector):
        super().__init__()
        self.image_encoder = image_encoder
        self.projector = projector
        self.llm = llm
        self.cfg = cfg

    def _merge_input_ids_with_image_features(self, image_features, inputs_embeds, input_ids, attention_mask=None):
        _, num_image_patches, embed_dim = image_features.shape
        batch_size, sequence_length = input_ids.shape
        # 1. Create a mask to know where special image tokens are
        special_image_token_mask = input_ids == self.cfg.image_token_index
        max_embed_dim = num_image_patches - 1 + sequence_length
        batch_indices, non_image_indices = torch.where(input_ids != self.cfg.image_token_index)
        # 2. Compute the positions where text should be written
        new_token_positions = torch.cumsum((special_image_token_mask * (num_image_patches - 1) + 1), -1) - 1
        text_to_overwrite = new_token_positions[batch_indices, non_image_indices]
        # 3. Create the full embedding, already padded to the maximum position
        final_embedding = torch.zeros(batch_size, max_embed_dim, embed_dim, dtype=inputs_embeds.dtype, device=inputs_embeds.device)
        final_attention_mask = torch.zeros(batch_size, max_embed_dim, dtype=attention_mask.dtype, device=inputs_embeds.device)
        # 4. Fill the embeddings based on the mask
        final_embedding[batch_indices, text_to_overwrite] = inputs_embeds[batch_indices, non_image_indices]
        final_attention_mask[batch_indices, text_to_overwrite] = attention_mask[batch_indices, non_image_indices]
        # 5. Fill the embeddings corresponding to the images.
        image_to_overwrite = torch.full((batch_size, max_embed_dim), True, dtype=torch.bool, device=inputs_embeds.device)
        image_to_overwrite[batch_indices, text_to_overwrite] = False
        final_embedding[image_to_overwrite] = image_features.reshape(-1, embed_dim)
        final_attention_mask |= image_to_overwrite
        position_ids = (final_attention_mask.cumsum(-1) - 1).masked_fill_((final_attention_mask == 0), 1)
        # 6. Mask out the embedding at padding positions
        batch_indices, pad_indices = torch.where(input_ids == self.cfg.pad_token_id)
        indices_to_mask = new_token_positions[batch_indices, pad_indices]
        final_embedding[batch_indices, indices_to_mask] = 0

        return final_embedding, final_attention_mask, position_ids

    def forward(self, input_ids, pixel_values=None, attention_mask=None, position_ids=None, past_key_values=None, labels=None):
        inputs_embeds = self.llm.embed_tokens(input_ids)
        # Merge text and images
        if pixel_values is not None and input_ids.shape[1] != 1:
            hidden_states = self.image_encoder.pre_layrnorm(self.image_encoder.embeddings(pixel_values))
            # this is not memory efficient at all (output_hidden_states=True) will save all the hidden states.
            selected_image_feature = self.image_encoder.encoder(hidden_states, output_hidden_states=True)[self.cfg.feature_layer][:, 1:]
            image_features = self.projector(selected_image_feature)
            inputs_embeds, attention_mask, position_ids = self._merge_input_ids_with_image_features(
                image_features, inputs_embeds, input_ids, attention_mask
            )
        # generation with cache
        elif past_key_values is not None and pixel_values is None and input_ids.shape[1] == 1:
            first_layer_past_key_value = past_key_values[0][0][:, :, :, 0]
            batch_index, non_attended_token_ids = torch.where(first_layer_past_key_value.float().sum(-2) == 0)
            past_length = first_layer_past_key_value.shape[-1]
            extended_attention_mask = torch.ones((attention_mask.shape[0], past_length), dtype=attention_mask.dtype, device=attention_mask.device)

            # Filter out only the tokens that can be un-attended, this can happen
            # if one uses Llava + Fused modules where the cache on the
            # first iteration is already big enough, or if one passes custom cache
            valid_indices = non_attended_token_ids < extended_attention_mask.size(-1)
            new_batch_index = batch_index[valid_indices]
            new_non_attended_tokens = non_attended_token_ids[valid_indices]

            # Zero-out the places where we don't need to attend
            extended_attention_mask[new_batch_index, new_non_attended_tokens] = 0

            attention_mask = torch.cat((extended_attention_mask, attention_mask[:, -1:]), dim=1)
            position_ids = torch.sum(attention_mask, dim=1).unsqueeze(-1) - 1

        outputs = self.llm(
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=self.cfg.use_cache,
            return_dict=True
        )
        logits = outputs[0]
        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            if attention_mask is not None:
                shift_attention_mask = attention_mask[..., 1:]
                shift_logits = logits[..., :-1, :][shift_attention_mask.to(logits.device) != 0].contiguous()
                shift_labels = labels[..., 1:][shift_attention_mask.to(labels.device) != 0].contiguous()
            else:
                shift_logits = logits[..., :-1, :].contiguous()
                shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1).to(shift_logits.device)
            )

        return MyLlavaOutput(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, inputs_embeds=None, pixel_values=None, attention_mask=None, **kwargs
    ):
        if past_key_values is not None:
            if isinstance(past_key_values, Cache):
                cache_length = past_key_values.get_seq_length()
                past_length = past_key_values.seen_tokens
            else:
                cache_length = past_length = past_key_values[0][0].shape[2]

            # Keep only the unprocessed tokens:
            # 1 - If the length of the attention_mask exceeds the length of input_ids, then we are in a setting where
            # some of the inputs are exclusively passed as part of the cache (e.g. when passing input_embeds as
            # input)
            if attention_mask is not None and attention_mask.shape[1] > input_ids.shape[1]:
                input_ids = input_ids[:, -(attention_mask.shape[1] - past_length) :]
            # 2 - If the past_length is smaller than input_ids', then input_ids holds all input tokens. We can discard
            # input_ids based on the past_length.
            elif past_length < input_ids.shape[1]:
                input_ids = input_ids[:, past_length:]
            # 3 - Otherwise (past_length >= input_ids.shape[1]), let's assume input_ids only has unprocessed tokens.
            elif self.config.image_token_index in input_ids:
                input_ids = input_ids[:, input_ids.shape[1] - 1 :]
            # If the cache has seen more tokens than it can hold, then the cache has a size limit. Let's discard the
            # older attention values, as their corresponding values are not part of the input.
            if cache_length < past_length and attention_mask is not None:
                attention_mask = attention_mask[:, -(cache_length + input_ids.shape[1]) :]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -input_ids.shape[1] :]

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
                "pixel_values": pixel_values,
            }
        )
        return model_inputs
