import torch
import yaml

print(torch.__version__)
import torch.optim as optim
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from torchvision import datasets
from torch.utils.tensorboard import SummaryWriter
import torch.nn.functional as F

from models.resnet_simclr import ResNetSimCLR
from utils import get_negative_mask, get_augmentation_transform

torch.manual_seed(0)

config = yaml.load(open("config.yaml", "r"), Loader=yaml.FullLoader)

batch_size = config['batch_size']
out_dim = config['out_dim']
temperature = config['temperature']
use_cosine_similarity = config['use_cosine_similarity']

data_augment = get_augmentation_transform(s=config['s'])

train_dataset = datasets.STL10('./data', split='train', download=True, transform=transforms.ToTensor())
train_loader = DataLoader(train_dataset, batch_size=batch_size, num_workers=config['num_workers'], drop_last=True,
                          shuffle=True)

# model = Encoder(out_dim=out_dim)
model = ResNetSimCLR(base_model=config["base_convnet"], out_dim=out_dim)

train_gpu = torch.cuda.is_available()
print("Is gpu available:", train_gpu)

# moves the model paramemeters to gpu
if train_gpu:
    model.cuda()

criterion = torch.nn.CrossEntropyLoss(reduction='sum')
optimizer = optim.Adam(model.parameters(), 3e-4)

train_writer = SummaryWriter()

if use_cosine_similarity:
    cos_similarity_dim1 = torch.nn.CosineSimilarity(dim=1)
    cos_similarity_dim2 = torch.nn.CosineSimilarity(dim=2)

# Mask to remove positive examples from the batch of negative samples
negative_mask = get_negative_mask(batch_size)

n_iter = 0
for e in range(config['epochs']):
    for step, (batch_x, _) in enumerate(train_loader):

        optimizer.zero_grad()

        xis = []
        xjs = []

        # draw two augmentation functions t , t' and apply separately for each input example
        for k in range(len(batch_x)):
            xis.append(data_augment(batch_x[k]))  # the first augmentation
            xjs.append(data_augment(batch_x[k]))  # the second augmentation

        xis = torch.stack(xis)
        xjs = torch.stack(xjs)

        if train_gpu:
            xis = xis.cuda()
            xjs = xjs.cuda()

        # get the representations and the projections
        ris, zis = model(xis)  # [N,C]
        train_writer.add_histogram("xi_repr", ris, global_step=n_iter)
        train_writer.add_histogram("xi_latent", zis, global_step=n_iter)

        # get the representations and the projections
        rjs, zjs = model(xjs)  # [N,C]
        train_writer.add_histogram("xj_repr", rjs, global_step=n_iter)
        train_writer.add_histogram("xj_latent", zjs, global_step=n_iter)

        # normalize projection feature vectors
        zis = F.normalize(zis, dim=1)
        zjs = F.normalize(zjs, dim=1)
        # assert zis.shape == (batch_size, out_dim), "Shape not expected: " + str(zis.shape)
        # assert zjs.shape == (batch_size, out_dim), "Shape not expected: " + str(zjs.shape)

        # positive pairs
        if use_cosine_similarity:
            l_pos = cos_similarity_dim1(zis.view(batch_size, out_dim), zjs.view(batch_size, out_dim)).view(batch_size,
                                                                                                           1)
        else:
            l_pos = torch.bmm(zis.view(batch_size, 1, out_dim), zjs.view(batch_size, out_dim, 1)).view(batch_size, 1)

        l_pos /= temperature
        # assert l_pos.shape == (batch_size, 1), "l_pos shape not valid" + str(l_pos.shape)  # [N,1]

        negatives = torch.cat([zjs, zis], dim=0)

        loss = 0

        for positives in [zis, zjs]:

            if use_cosine_similarity:
                negatives = negatives.view(1, (2 * batch_size), out_dim)
                l_neg = cos_similarity_dim2(positives.view(batch_size, 1, out_dim), negatives)
            else:
                l_neg = torch.tensordot(positives.view(batch_size, 1, out_dim),
                                        negatives.T.view(1, out_dim, (2 * batch_size)),
                                        dims=2)

            labels = torch.zeros(batch_size, dtype=torch.long)
            if train_gpu:
                labels = labels.cuda()

            l_neg = l_neg[negative_mask].view(l_neg.shape[0], -1)
            l_neg /= temperature

            # assert l_neg.shape == (batch_size, 2 * (batch_size - 1)), "Shape of negatives not expected." + str(
            #     l_neg.shape)
            logits = torch.cat([l_pos, l_neg], dim=1)  # [N,K+1]
            loss += criterion(logits, labels)

        loss = loss / (2 * batch_size)
        train_writer.add_scalar('loss', loss, global_step=n_iter)

        loss.backward()
        optimizer.step()
        n_iter += 1
        # print("Step {}, Loss {}".format(step, loss))

torch.save(model.state_dict(), './checkpoints/checkpoint.pth')
